"""Runtime configuration.

All paths and the total privacy budget can be overridden with environment
variables so the container does not need code changes to be relocated.

The administrator token is read from the environment *only*.  It is never
written to the database, to logs or to the OpenAPI document, and it has no
default: an unset token disables the administrator endpoints rather than
falling back to a guessable value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

#: Total lifetime privacy budget.  Parsed straight into a ``Decimal`` from the
#: environment string so no float rounding ever touches budget arithmetic.
DEFAULT_TOTAL_BUDGET = "1"

#: CSV that is imported on first start when no data set is present.
DEFAULT_CSV_PATH = "seed/followup.csv"

#: Environment variable holding the administrator bearer token.
ADMIN_TOKEN_ENV = "ADMIN_TOKEN"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value


def _optional_env(name: str) -> str:
    value = os.environ.get(name)
    return "" if value is None else value.strip()


@dataclass(frozen=True)
class Settings:
    database_path: Path
    csv_path: Path
    total_budget: Decimal
    import_on_startup: bool
    sqlite_busy_timeout_ms: int
    #: Bearer token required by the administrator endpoints.  Empty means the
    #: endpoints are disabled (503); there is deliberately no built-in default.
    admin_token: str = field(default="", repr=False)
    #: Accepted for deployment parity; request handlers are threadpool tasks
    #: whose write path is serialised by SQLite's ``BEGIN IMMEDIATE``, so this
    #: value documents intent rather than gating anything.
    max_concurrency: int = 4

    @classmethod
    def from_env(cls) -> "Settings":
        total = Decimal(_env("TOTAL_BUDGET", DEFAULT_TOTAL_BUDGET))
        if not total.is_finite() or total <= 0:
            raise ValueError("TOTAL_BUDGET must be a positive finite number")
        return cls(
            database_path=Path(_env("DATABASE_PATH", "data/followup.db")),
            csv_path=Path(_env("CSV_PATH", DEFAULT_CSV_PATH)),
            total_budget=total,
            import_on_startup=_env("IMPORT_ON_STARTUP", "1").lower()
            not in {"0", "false", "no"},
            sqlite_busy_timeout_ms=int(_env("SQLITE_BUSY_TIMEOUT_MS", "30000")),
            admin_token=_optional_env(ADMIN_TOKEN_ENV),
            max_concurrency=max(1, int(_env("MAX_CONCURRENCY", "4"))),
        )
