"""HTTP interface (FastAPI).

Endpoints
---------
``GET  /health``                  liveness plus whether a data set is loaded
``GET  /v1/budget``               remaining global privacy budget
``POST /v1/counts``               privatized count for one (region, outcome)
``GET  /v1/requests/{id}``        look up a previously released answer
``POST /v1/admin/withdrawals``    administrator-only subject withdrawal

Only noisy counts and budget figures ever leave the process; exact counts are
never part of any response model.  The administrator endpoint never reveals
whether a subject id exists or was already withdrawn: every accepted request
gets the same fixed receipt, and authentication failure changes no data.
"""

from __future__ import annotations

import logging
import re
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import Settings
from .db import Database
from .errors import BudgetExhausted, DatasetMissing, RequestIdConflict
from .importer import ImportError_, import_csv
from .models import SqliteRepository
from .privacy import MIN_EPSILON, canonical_decimal, canonical_epsilon
from .service import QueryService, SnapshotUnstable

logger = logging.getLogger("followup_counts")

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REGION_MAX_LEN = 128
SUBJECT_ID_MAX_LEN = 256
MAX_EPSILON = Decimal("1")

#: Fixed, state-independent receipt returned by every successful withdrawal.
#: It deliberately does not echo the subject id, repeat the status of the
#: ledger or say whether anything changed: "unknown id", "already withdrawn"
#: and "just recorded" are indistinguishable to the caller.
WITHDRAWAL_RECEIPT = {"status": "accepted"}

_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="AdminToken",
    description=(
        "Administrator bearer token. Configured with the ADMIN_TOKEN "
        "environment variable; never stored in the database or the API."
    ),
)



# --------------------------------------------------------------------------
# request / response models
# --------------------------------------------------------------------------
class CountQuery(BaseModel):
    """Parameters of a single privatized count query."""

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {
                    "request_id": "req-0001",
                    "region": "north",
                    "outcome": 1,
                    "epsilon": "0.1",
                }
            ]
        },
    }

    request_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Client-generated idempotency key (letters, digits, . _ : -).",
    )
    region: str = Field(
        ...,
        min_length=1,
        max_length=REGION_MAX_LEN,
        description="Region exactly as it appears in the imported CSV.",
    )
    outcome: int = Field(
        ...,
        strict=True,
        ge=0,
        le=1,
        description="Follow-up outcome: 0 or 1.",
    )
    epsilon: Decimal = Field(
        ...,
        gt=0,
        le=1,
        description="Privacy budget for this answer; Laplace scale is 1/epsilon.",
    )

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "request_id must start with a letter or digit and contain only "
                "letters, digits, '.', '_', ':' or '-'"
            )
        return value

    @field_validator("region")
    @classmethod
    def _valid_region(cls, value: str) -> str:
        if not value:
            raise ValueError("region must not be empty")
        if value != value.strip():
            raise ValueError("region must not have leading or trailing whitespace")
        return value

    @field_validator("epsilon")
    @classmethod
    def _valid_epsilon(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("epsilon must be finite")
        try:
            canonical = canonical_epsilon(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not (MIN_EPSILON <= value <= MAX_EPSILON):
            raise ValueError("epsilon is outside the representable range")
        return Decimal(canonical)


class CountResponse(BaseModel):
    request_id: str
    region: str
    outcome: int
    epsilon: str = Field(description="Canonical epsilon charged for this answer.")
    noisy_count: int = Field(
        description="Laplace-perturbed count (never the exact count)."
    )
    remaining_budget: str = Field(
        description="Global privacy budget left after this query, as a decimal."
    )
    replayed: bool = Field(
        description="True when a stored answer was reused; no budget charged."
    )
    charged: bool = Field(description="True when this call spent budget.")


class BudgetResponse(BaseModel):
    total_budget: str
    remaining_budget: str


class HealthResponse(BaseModel):
    status: str
    version: str
    dataset_loaded: bool
    row_count: int | None = None


class WithdrawalRequest(BaseModel):
    """Administrator instruction to exclude one subject from future counts."""

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"subject_id": "S0001"}]},
    }

    subject_id: str = Field(
        ...,
        min_length=1,
        max_length=SUBJECT_ID_MAX_LEN,
        description=(
            "Subject identifier exactly as it appears in the imported CSV. "
            "The response is the same whether or not the id is present."
        ),
    )

    @field_validator("subject_id")
    @classmethod
    def _valid_subject_id(cls, value: str) -> str:
        if not value:
            raise ValueError("subject_id must not be empty")
        if value != value.strip():
            raise ValueError(
                "subject_id must not have leading or trailing whitespace"
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("subject_id must not contain control characters")
        return value


class WithdrawalResponse(BaseModel):
    status: str = Field(
        description=(
            "Always 'accepted'. Identical for every accepted request so the "
            "response reveals nothing about whether the subject exists or had "
            "already withdrawn."
        )
    )


class ErrorBody(BaseModel):
    error: str
    message: str


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, **extra},
    )


def _dataset_info(database: Database) -> tuple[bool, int | None]:
    conn = database.connect()
    row = conn.execute("SELECT row_count FROM dataset WHERE id = 1").fetchone()
    return (row is not None, int(row["row_count"]) if row else None)


# --------------------------------------------------------------------------
# application factory
# --------------------------------------------------------------------------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path, settings.sqlite_busy_timeout_ms)
    repository = SqliteRepository(database)
    service = QueryService(repository, settings.total_budget)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialise()
        if settings.import_on_startup:
            try:
                result = import_csv(
                    database, settings.csv_path, settings.total_budget
                )
                logger.info(
                    "dataset ready: rows=%d imported_now=%s",
                    result.row_count,
                    result.imported,
                )
            except ImportError_ as exc:
                # A missing or malformed seed is not fatal: the API reports
                # "no dataset" rather than refusing to boot, which keeps
                # health checks and Swagger reachable for operators.
                logger.warning("CSV import skipped: %s", exc)
        yield
        database.close_thread_connection()

    app = FastAPI(
        title="Follow-up Count Privacy API",
        version=__version__,
        description=(
            "Differentially private follow-up counts.\n\n"
            "The data set is imported once at startup and is never replaced. "
            "Each successful query releases a Laplace-noised count "
            "(scale = 1/epsilon, sensitivity 1) and charges epsilon against a "
            "global budget of 1.0. Retrying the same `request_id` with "
            "identical parameters returns the original answer for free; "
            "reusing the id with different parameters is rejected with 409. "
            "Invalid requests and failed saves never spend budget.\n\n"
            "Administrators can exclude a subject from all future counts via "
            "`POST /v1/admin/withdrawals`, authenticated with the bearer token "
            "configured through the `ADMIN_TOKEN` environment variable. The "
            "receipt is identical whether or not the subject exists, and "
            "released answers, replays and the spent budget are never "
            "rewritten."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.service = service
    app.state.repository = repository

    async def require_admin(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> None:
        """Reject anything but the configured bearer token.

        The comparison is constant time.  An unset token disables the
        endpoint entirely instead of accepting a default, and a missing,
        malformed or wrong token produces the same ``401``.  No database work
        happens before this check succeeds.
        """
        expected = settings.admin_token
        if not expected:
            raise HTTPException(
                status_code=403,
                detail="administrator endpoints are disabled",
            )
        supplied = credentials.credentials if credentials is not None else ""
        if not secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid administrator token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # -- endpoints ---------------------------------------------------------
    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Liveness and data-set availability",
        tags=["operations"],
    )
    def health() -> HealthResponse:
        loaded, row_count = _dataset_info(database)
        return HealthResponse(
            status="ok",
            version=__version__,
            dataset_loaded=loaded,
            row_count=row_count,
        )

    @app.get(
        "/v1/budget",
        response_model=BudgetResponse,
        summary="Remaining global privacy budget",
        tags=["privacy"],
        responses={503: {"model": ErrorBody, "description": "No data set imported"}},
    )
    def budget():
        try:
            snapshot = repository.budget_snapshot()
        except DatasetMissing as exc:
            return _error(503, "dataset_missing", str(exc))
        return BudgetResponse(
            total_budget=canonical_decimal(snapshot.total),
            remaining_budget=canonical_decimal(snapshot.remaining),
        )

    @app.post(
        "/v1/counts",
        response_model=CountResponse,
        summary="Privatized count for a region and outcome",
        tags=["queries"],
        responses={
            200: {"description": "Fresh release or idempotent replay"},
            409: {"model": ErrorBody, "description": "Request id parameter conflict"},
            422: {"model": ErrorBody, "description": "Invalid request"},
            429: {"model": ErrorBody, "description": "Privacy budget exhausted"},
            500: {"model": ErrorBody, "description": "Storage failure; nothing charged"},
            503: {
                "model": ErrorBody,
                "description": "No data set imported, or snapshot kept changing",
            },
        },
    )
    def counts(body: CountQuery):
        try:
            outcome = service.query(
                request_id=body.request_id,
                region=body.region,
                outcome=body.outcome,
                epsilon=body.epsilon,
            )
        except RequestIdConflict as exc:
            return _error(409, "request_id_conflict", str(exc))
        except BudgetExhausted as exc:
            return _error(
                429,
                "budget_exhausted",
                f"requested epsilon {exc.requested} exceeds remaining "
                f"budget {exc.remaining}",
                remaining_budget=canonical_decimal(exc.remaining),
            )
        except DatasetMissing as exc:
            return _error(503, "dataset_missing", str(exc))
        except SnapshotUnstable as exc:
            return _error(503, "snapshot_unstable", str(exc))

        return CountResponse(
            request_id=body.request_id,
            region=body.region,
            outcome=body.outcome,
            epsilon=outcome.epsilon,
            noisy_count=outcome.noisy_count,
            remaining_budget=canonical_decimal(outcome.remaining_budget),
            replayed=outcome.replayed,
            charged=outcome.charged,
        )

    @app.get(
        "/v1/requests/{request_id}",
        response_model=CountResponse,
        summary="Replay a previously released answer",
        tags=["queries"],
        responses={404: {"model": ErrorBody, "description": "Unknown request id"}},
    )
    def get_request(request_id: str):
        lookup = repository.lookup(request_id)
        if lookup.stored is None:
            return _error(
                404, "request_not_found", "no released answer for this request id"
            )
        stored = lookup.stored
        return CountResponse(
            request_id=stored.request_id,
            region=stored.region,
            outcome=stored.outcome,
            epsilon=stored.epsilon,
            noisy_count=stored.noisy_count,
            remaining_budget=canonical_decimal(lookup.remaining_budget),
            replayed=True,
            charged=False,
        )

    @app.post(
        "/v1/admin/withdrawals",
        response_model=WithdrawalResponse,
        status_code=200,
        summary="Withdraw a subject from all future counts",
        tags=["administration"],
        dependencies=[Depends(require_admin)],
        responses={
            200: {"description": "Withdrawal accepted (state-independent receipt)"},
            401: {"model": ErrorBody, "description": "Missing or invalid token"},
            403: {"model": ErrorBody, "description": "Administrator API disabled"},
            422: {"model": ErrorBody, "description": "Invalid request"},
            500: {"model": ErrorBody, "description": "Storage failure; nothing changed"},
        },
    )
    def withdraw(body: WithdrawalRequest):
        """Record that a subject withdrew consent.

        Takes effect for every count whose snapshot is taken after this
        request commits.  Already-released answers, request-id replays and the
        spent privacy budget are left exactly as they were; only the ledger is
        appended.  A write failure rolls the transaction back and is reported
        as ``500`` without changing any table.

        The subject id is deliberately not echoed and no status detail is
        returned: an unknown id, an already-withdrawn id and a fresh id all
        produce the identical receipt.
        """
        # Any storage failure propagates to the uniform 500 handler.  The
        # ledger insert and its counter bump share one transaction, so a
        # failure has already rolled back and changed nothing.
        receipt = repository.record_withdrawal(body.subject_id)

        logger.info(
            "withdrawal recorded: subject=%r newly_recorded=%s sequence=%d",
            receipt.subject_id,
            receipt.newly_recorded,
            receipt.sequence,
        )
        return WithdrawalResponse(**WITHDRAWAL_RECEIPT)

    # -- uniform error shape ----------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = []
        for error in exc.errors():
            if isinstance(error, dict):
                location = error.get("loc", ())
                message = error.get("msg", "")
                kind = error.get("type", "")
            else:  # pragma: no cover - defensive for future FastAPI versions
                location = getattr(error, "loc", ())
                message = getattr(error, "msg", "")
                kind = getattr(error, "type", "")
            details.append(
                {
                    "location": list(location),
                    "message": message,
                    "type": kind,
                }
            )
        return _error(
            422,
            "invalid_request",
            "request validation failed",
            details=details,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            code = "unauthorized"
        elif exc.status_code == 403:
            code = "admin_disabled"
        else:  # pragma: no cover - no other status is raised explicitly
            code = "http_error"
        headers = getattr(exc, "headers", None) or {}
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": str(exc.detail)},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(500, "internal_error", "internal server error")

    return app


app = create_app()
