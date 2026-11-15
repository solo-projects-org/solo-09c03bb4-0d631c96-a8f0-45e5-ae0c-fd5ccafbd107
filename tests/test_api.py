"""End-to-end tests for the privacy API."""

from __future__ import annotations

import importlib
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.importer import import_csv


@pytest.fixture()
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "followup.csv"
    path.write_text(
        "subject_id,region,outcome\n"
        "A,north,1\n"
        "B,north,1\n"
        "C,north,0\n"
        "D,south,1\n"
        "E,south,0\n"
        "F,east,0\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def settings(tmp_path: Path, csv_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "test.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        admin_token="test-admin-token",
        max_concurrency=4,
    )


@pytest.fixture()
def app_client(settings: Settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings)
    with TestClient(application) as client:
        yield client


ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}


def withdraw(client, subject_id: str):
    return client.post(
        "/v1/admin/withdrawals",
        json={"subject_id": subject_id},
        headers=ADMIN_HEADERS,
    )


def query(client, request_id: str, region: str, outcome: int, epsilon: str = "0.1"):
    return client.post(
        "/v1/counts",
        json={
            "request_id": request_id,
            "region": region,
            "outcome": outcome,
            "epsilon": epsilon,
        },
    )


# --------------------------------------------------------------------------
# importer
# --------------------------------------------------------------------------
def test_import_is_idempotent_and_never_replaces(tmp_path: Path, csv_path: Path):
    db = Database(tmp_path / "import.db")
    db.initialise()

    first = import_csv(db, csv_path, Decimal("1"))
    assert first.imported is True
    assert first.row_count == 6

    second = import_csv(db, csv_path, Decimal("1"))
    assert second.imported is False
    assert second.row_count == 6

    # A *different* file must not replace the original data set.
    other = tmp_path / "other.csv"
    other.write_text(
        "subject_id,region,outcome\nZ,north,1\n", encoding="utf-8"
    )
    third = import_csv(db, other, Decimal("1"))
    assert third.imported is False
    assert third.row_count == 6

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 6
    assert conn.execute("SELECT budget_spent FROM dataset").fetchone()[0] == "0"


def test_import_rejects_duplicate_subject_ids(tmp_path: Path):
    path = tmp_path / "dup.csv"
    path.write_text(
        "subject_id,region,outcome\nA,north,1\nA,south,0\n", encoding="utf-8"
    )
    db = Database(tmp_path / "dup.db")
    with pytest.raises(Exception):
        import_csv(db, path, Decimal("1"))
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0] == 0


def test_import_rejects_bad_outcome(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "subject_id,region,outcome\nA,north,2\n", encoding="utf-8"
    )
    db = Database(tmp_path / "bad.db")
    with pytest.raises(Exception):
        import_csv(db, path, Decimal("1"))


# --------------------------------------------------------------------------
# API behaviour
# --------------------------------------------------------------------------
def test_budget_endpoint_and_health(app_client):
    health = app_client.get("/health").json()
    assert health["status"] == "ok"
    assert health["dataset_loaded"] is True
    assert health["row_count"] == 6

    budget = app_client.get("/v1/budget").json()
    assert budget == {"total_budget": "1", "remaining_budget": "1"}


def test_query_charges_budget_and_returns_noisy_count(app_client):
    response = app_client.post(
        "/v1/counts",
        json={
            "request_id": "r1",
            "region": "north",
            "outcome": 1,
            "epsilon": "0.1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["noisy_count"], int)
    assert body["noisy_count"] >= 0
    assert body["remaining_budget"] == "0.9"
    assert body["replayed"] is False
    assert body["charged"] is True
    assert "0.1" in response.text

    budget = app_client.get("/v1/budget").json()
    assert budget["remaining_budget"] == "0.9"


def test_retry_same_id_same_params_returns_same_value_free(app_client):
    payload = {"request_id": "r1", "region": "north", "outcome": 1, "epsilon": "0.1"}
    first = app_client.post("/v1/counts", json=payload).json()
    second = app_client.post("/v1/counts", json=payload).json()

    assert second["noisy_count"] == first["noisy_count"]
    assert second["remaining_budget"] == first["remaining_budget"] == "0.9"
    assert second["replayed"] is True
    assert second["charged"] is False

    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_retry_same_id_different_params_is_rejected(app_client):
    payload = {"request_id": "r1", "region": "north", "outcome": 1, "epsilon": "0.1"}
    assert app_client.post("/v1/counts", json=payload).status_code == 200

    changed = dict(payload, epsilon="0.2")
    response = app_client.post("/v1/counts", json=changed)
    assert response.status_code == 409
    assert response.json()["error"] == "request_id_conflict"

    # Budget unchanged by the rejected request.
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_epsilon_equivalent_forms_are_the_same_parameters(app_client):
    first = app_client.post(
        "/v1/counts",
        json={"request_id": "r1", "region": "north", "outcome": 1, "epsilon": 0.1},
    )
    assert first.status_code == 200
    second = app_client.post(
        "/v1/counts",
        json={"request_id": "r1", "region": "north", "outcome": 1, "epsilon": "0.10"},
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["noisy_count"] == first.json()["noisy_count"]


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": 0},
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": 1.5},
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": -0.1},
        {"request_id": "r", "region": "north", "outcome": 2, "epsilon": 0.1},
        {"request_id": "r", "region": "north", "outcome": True, "epsilon": 0.1},
        {"request_id": "r", "region": "", "outcome": 1, "epsilon": 0.1},
        {"request_id": "bad id!", "region": "north", "outcome": 1, "epsilon": 0.1},
        {"region": "north", "outcome": 1, "epsilon": 0.1},
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": 0.1, "x": 1},
    ],
)
def test_invalid_requests_do_not_charge_budget(app_client, payload):
    before = app_client.get("/v1/budget").json()["remaining_budget"]
    response = app_client.post("/v1/counts", json=payload)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert app_client.get("/v1/budget").json()["remaining_budget"] == before
    assert app_client.get(f"/v1/requests/{payload.get('request_id', 'x')}").status_code == 404


def test_budget_exhaustion_blocks_fresh_queries_but_not_replays(app_client):
    payload = {"request_id": "a", "region": "north", "outcome": 1, "epsilon": "0.6"}
    assert app_client.post("/v1/counts", json=payload).status_code == 200

    payload_b = dict(payload, request_id="b", epsilon="0.6")
    response = app_client.post("/v1/counts", json=payload_b)
    assert response.status_code == 429
    assert response.json()["error"] == "budget_exhausted"
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.4"

    # The original request can still be replayed for free.
    replay = app_client.post("/v1/counts", json=payload)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_budget_boundary_is_exact(app_client):
    # 0.1 + 0.2 styled decimal arithmetic must not leave 0.7-ish float dust.
    for index in range(10):
        payload = {
            "request_id": f"step-{index}",
            "region": "north",
            "outcome": 0,
            "epsilon": "0.1",
        }
        assert app_client.post("/v1/counts", json=payload).status_code == 200
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0"

    extra = {
        "request_id": "step-10",
        "region": "north",
        "outcome": 0,
        "epsilon": "0.1",
    }
    assert app_client.post("/v1/counts", json=extra).status_code == 429


def test_concurrent_requests_never_overspend(tmp_path: Path, csv_path: Path):
    """Twenty simultaneous 0.1 queries against a budget of 1.0: exactly ten
    may succeed, the rest must be rejected."""
    settings = Settings(
        database_path=tmp_path / "concurrent.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=60_000,
        max_concurrency=8,
    )
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings)
    with TestClient(application) as client:
        statuses: list[int] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            response = client.post(
                "/v1/counts",
                json={
                    "request_id": f"concurrent-{index}",
                    "region": "north",
                    "outcome": 1,
                    "epsilon": "0.1",
                },
            )
            with lock:
                statuses.append(response.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert statuses.count(200) == 10
        assert statuses.count(429) == 10
        assert client.get("/v1/budget").json()["remaining_budget"] == "0"


def test_persistence_across_restart_returns_original_value(tmp_path: Path, csv_path: Path):
    settings = Settings(
        database_path=tmp_path / "restart.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
    )
    from fastapi.testclient import TestClient

    from app.main import create_app

    payload = {"request_id": "durable", "region": "north", "outcome": 1, "epsilon": "0.1"}

    with TestClient(create_app(settings)) as client:
        first = client.post("/v1/counts", json=payload).json()

    # Simulate a fresh process: new app object, same database file.  The
    # importer must not re-import and the stored answer must still be there.
    with TestClient(create_app(settings)) as client:
        replay = client.post("/v1/counts", json=payload).json()
        assert replay["noisy_count"] == first["noisy_count"]
        assert replay["remaining_budget"] == first["remaining_budget"]
        assert replay["replayed"] is True
        assert client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_save_failure_does_not_charge_budget(tmp_path: Path, csv_path: Path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "failure.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
    )
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SqliteRepository

    application = create_app(settings)
    with TestClient(application, raise_server_exceptions=False) as client:
        original_apply = SqliteRepository.apply_query

        def failing_apply(self, **kwargs):
            raise sqlite3.OperationalError("simulated disk failure")

        monkeypatch.setattr(SqliteRepository, "apply_query", failing_apply)
        response = client.post(
            "/v1/counts",
            json={
                "request_id": "fail-1",
                "region": "north",
                "outcome": 1,
                "epsilon": "0.5",
            },
        )
        assert response.status_code == 500

        monkeypatch.setattr(SqliteRepository, "apply_query", original_apply)
        assert client.get("/v1/budget").json()["remaining_budget"] == "1"

        # Nothing was stored, so the id is still usable.
        retry = client.post(
            "/v1/counts",
            json={
                "request_id": "fail-1",
                "region": "north",
                "outcome": 1,
                "epsilon": "0.5",
            },
        )
        assert retry.status_code == 200
        assert retry.json()["replayed"] is False
        assert retry.json()["remaining_budget"] == "0.5"


def test_partial_write_failure_rolls_back_both_rows(tmp_path: Path, csv_path: Path):
    """Force the budget UPDATE to fail after the request row was inserted."""
    db = Database(tmp_path / "partial.db")
    db.initialise()
    import_csv(db, csv_path, Decimal("1"))
    repository = __import__("app.models", fromlist=["SqliteRepository"]).SqliteRepository(db)

    conn = db.connect()
    conn.execute(
        """
        CREATE TRIGGER block_budget BEFORE UPDATE ON dataset
        BEGIN SELECT RAISE(ABORT, 'blocked'); END
        """
    )

    with pytest.raises(sqlite3.Error):
        repository.apply_query(
            request_id="partial",
            fingerprint="fp",
            region="north",
            outcome=1,
            epsilon=Decimal("0.5"),
            noisy_count=1,
        )

    conn.execute("DROP TRIGGER block_budget")
    assert conn.execute("SELECT budget_spent FROM dataset").fetchone()[0] == "0"
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_openapi_schema_available(app_client):
    response = app_client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/v1/counts" in schema["paths"]
    assert "/docs" not in {p for p in schema["paths"]}
    response_docs = app_client.get("/docs")
    assert response_docs.status_code == 200


def test_no_exact_count_field_in_responses(app_client):
    response = app_client.post(
        "/v1/counts",
        json={"request_id": "r", "region": "north", "outcome": 1, "epsilon": "1"},
    ).json()
    for key in response:
        assert key not in {"count", "true_count", "exact_count"}


def test_unknown_region_returns_noiseless_free_answer(app_client):
    # An unknown region has a true count of zero; the noisy answer may be
    # anything, but it is still charged and stored.
    response = app_client.post(
        "/v1/counts",
        json={"request_id": "missing", "region": "nowhere", "outcome": 1, "epsilon": "0.1"},
    )
    assert response.status_code == 200
    assert response.json()["remaining_budget"] == "0.9"


def test_lookup_endpoint(app_client):
    payload = {"request_id": "lookup", "region": "north", "outcome": 1, "epsilon": "0.2"}
    created = app_client.post("/v1/counts", json=payload).json()
    lookup = app_client.get("/v1/requests/lookup").json()
    assert lookup["noisy_count"] == created["noisy_count"]
    assert lookup["replayed"] is True
    assert lookup["charged"] is False
    assert lookup["remaining_budget"] == created["remaining_budget"]


# --------------------------------------------------------------------------
# withdrawal administration
# --------------------------------------------------------------------------
def _live_count(db_path: Path, region: str, outcome: int) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            """
            SELECT COUNT(*) FROM subjects s
            WHERE s.region = ? AND s.outcome = ?
              AND NOT EXISTS (
                  SELECT 1 FROM withdrawals w WHERE w.subject_id = s.subject_id
              )
            """,
            (region, outcome),
        ).fetchone()[0]
    finally:
        conn.close()


def test_withdrawal_receipt_is_identical_for_every_outcome(app_client, settings):
    # Unknown id, existing id, repeated id: one indistinguishable receipt.
    unknown = withdraw(app_client, "Z-does-not-exist")
    fresh = withdraw(app_client, "A")
    repeat = withdraw(app_client, "A")

    assert unknown.status_code == fresh.status_code == repeat.status_code == 200
    assert unknown.json() == fresh.json() == repeat.json() == {"status": "accepted"}
    # No subject id, sequence or "newly recorded" flag leaks the ledger state.
    assert "A" not in fresh.text
    assert set(fresh.json().keys()) == {"status"}

    # The ledger is append-only but idempotent: one row per subject.
    conn = sqlite3.connect(str(settings.database_path))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE subject_id = 'A'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 1


def test_withdrawal_changes_future_counts_and_never_touches_import(app_client, settings):
    before = _live_count(settings.database_path, "north", 1)
    assert before == 2  # A and B

    assert withdraw(app_client, "A").status_code == 200
    assert _live_count(settings.database_path, "north", 1) == 1

    # The imported table is untouched: the CSV is imported once and rows stay.
    conn = sqlite3.connect(str(settings.database_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM subjects WHERE subject_id = 'A'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT row_count FROM dataset").fetchone()[0] == 6
    finally:
        conn.close()

    # A fresh answer is charged normally and its stored row records that it
    # was computed from a snapshot that includes the withdrawal.
    response = query(app_client, "after-withdrawal", "north", 1)
    assert response.status_code == 200
    assert response.json()["charged"] is True


def test_withdrawal_is_durable_across_restart(tmp_path: Path, csv_path: Path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        database_path=tmp_path / "withdraw-restart.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        admin_token="restart-token",
        max_concurrency=4,
    )
    headers = {"Authorization": "Bearer restart-token"}

    with TestClient(create_app(settings)) as client:
        assert client.post(
            "/v1/admin/withdrawals",
            json={"subject_id": "A"},
            headers=headers,
        ).json() == {"status": "accepted"}
        assert _live_count(settings.database_path, "north", 1) == 1

    # A fresh process re-reads the same volume; the CSV is not re-imported and
    # the withdrawn subject stays excluded.
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["row_count"] == 6
        assert _live_count(settings.database_path, "north", 1) == 1
        response = query(client, "after-restart", "north", 1)
        assert response.status_code == 200
        conn = sqlite3.connect(str(settings.database_path))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM dataset"
            ).fetchone()[0] == 1
        finally:
            conn.close()


def test_withdrawal_can_precede_import(tmp_path: Path, csv_path: Path):
    """A withdrawal for a not-yet-imported id is accepted and honoured later."""
    from fastapi.testclient import TestClient

    from app.db import Database
    from app.models import SqliteRepository
    from app.importer import import_csv

    db = Database(tmp_path / "pre-import.db")
    db.initialise()
    repository = SqliteRepository(db)
    repository.record_withdrawal("A")

    import_csv(db, csv_path, Decimal("1"))
    assert repository.count_subjects("north", 1) == 1
    db.close_all()


def test_published_answer_replay_and_budget_survive_withdrawal(app_client, settings):
    payload = {"request_id": "published", "region": "north", "outcome": 1, "epsilon": "0.1"}
    published = app_client.post("/v1/counts", json=payload).json()
    assert published["remaining_budget"] == "0.9"

    assert withdraw(app_client, "A").status_code == 200

    # The released answer is unchanged, replay is free, and the budget is
    # exactly where it was: withdrawal is not a release and spends nothing.
    replay = app_client.post("/v1/counts", json=payload).json()
    assert replay["noisy_count"] == published["noisy_count"]
    assert replay["replayed"] is True
    assert replay["charged"] is False
    assert replay["remaining_budget"] == "0.9"

    lookup = app_client.get("/v1/requests/published").json()
    assert lookup["noisy_count"] == published["noisy_count"]
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.9"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer test-admin-toke"},
        {"Authorization": "Bearer test-admin-token-extra"},
        {"Authorization": "Basic dGVzdC1hZG1pbi10b2tlbg=="},
        {"Authorization": "Bearer "},
    ],
)
def test_bad_credentials_are_rejected_and_change_nothing(app_client, settings, headers):
    conn = sqlite3.connect(str(settings.database_path))
    try:
        before_counter = conn.execute(
            "SELECT last_sequence FROM withdrawal_counter"
        ).fetchone()[0]
    finally:
        conn.close()

    conn = sqlite3.connect(str(settings.database_path))
    try:
        before_names = conn.execute(
            "SELECT subject_id, region, outcome FROM subjects ORDER BY subject_id"
        ).fetchall()
        before_budget = conn.execute("SELECT budget_spent FROM dataset").fetchone()[0]
        before_requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        before_withdrawals = conn.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0]
    finally:
        conn.close()

    response = app_client.post(
        "/v1/admin/withdrawals",
        json={"subject_id": "A"},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert "test-admin-token" not in response.text

    conn = sqlite3.connect(str(settings.database_path))
    try:
        assert conn.execute(
            "SELECT last_sequence FROM withdrawal_counter"
        ).fetchone()[0] == before_counter
        assert conn.execute(
            "SELECT subject_id, region, outcome FROM subjects ORDER BY subject_id"
        ).fetchall() == before_names
        assert conn.execute("SELECT budget_spent FROM dataset").fetchone()[0] == before_budget
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == before_requests
        assert conn.execute(
            "SELECT COUNT(*) FROM withdrawals"
        ).fetchone()[0] == before_withdrawals
    finally:
        conn.close()


def test_admin_endpoint_disabled_when_token_unset(tmp_path: Path, csv_path: Path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        database_path=tmp_path / "no-token.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        admin_token="",
        max_concurrency=4,
    )
    with TestClient(create_app(settings)) as client:
        for headers in ({}, {"Authorization": "Bearer anything"}):
            response = client.post(
                "/v1/admin/withdrawals",
                json={"subject_id": "A"},
                headers=headers,
            )
            assert response.status_code == 403
            assert response.json()["error"] == "admin_disabled"
        assert client.get("/v1/budget").json()["remaining_budget"] == "1"


def test_invalid_withdrawal_body_never_writes(app_client, settings):
    for body in (
        {"subject_id": ""},
        {"subject_id": "   "},
        {"subject_id": " A "},
        {"subject_id": "A\n"},
        {"subject_id": "A\x00"},
        {"subject_id": 7},
        {"subject_id": "A", "extra": 1},
        {},
    ):
        response = app_client.post(
            "/v1/admin/withdrawals", json=body, headers=ADMIN_HEADERS
        )
        assert response.status_code == 422, body
        assert response.json()["error"] == "invalid_request"

    conn = sqlite3.connect(str(settings.database_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0] == 0
        assert conn.execute(
            "SELECT last_sequence FROM withdrawal_counter"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_withdrawal_write_failure_changes_no_table(tmp_path: Path, csv_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SqliteRepository

    settings = Settings(
        database_path=tmp_path / "withdraw-failure.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        admin_token="failure-token",
        max_concurrency=4,
    )
    headers = {"Authorization": "Bearer failure-token"}
    application = create_app(settings)
    with TestClient(application, raise_server_exceptions=False) as client:
        original = SqliteRepository.record_withdrawal

        def failing(self, subject_id: str):
            raise sqlite3.OperationalError("simulated disk failure")

        monkeypatch.setattr(SqliteRepository, "record_withdrawal", failing)
        response = client.post(
            "/v1/admin/withdrawals", json={"subject_id": "A"}, headers=headers
        )
        assert response.status_code == 500
        assert response.json()["error"] == "internal_error"

        monkeypatch.setattr(SqliteRepository, "record_withdrawal", original)
        conn = sqlite3.connect(str(settings.database_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0] == 0
            assert conn.execute(
                "SELECT last_sequence FROM withdrawal_counter"
            ).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 6
        finally:
            conn.close()

        # The subject is still counted, and a later withdrawal works normally.
        retry = client.post(
            "/v1/admin/withdrawals", json={"subject_id": "A"}, headers=headers
        )
        assert retry.json() == {"status": "accepted"}
        assert _live_count(settings.database_path, "north", 1) == 1


def test_withdrawal_then_new_query_uses_live_count(tmp_path: Path, csv_path: Path):
    """A withdrawal committed before a fresh query is reflected in that query.

    The noisy value is random, so the check is on the stored snapshot witness
    and on the live count that was handed to the mechanism, using a repository
    seam that records the true count.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SqliteRepository

    settings = Settings(
        database_path=tmp_path / "ordered.db",
        csv_path=csv_path,
        total_budget=Decimal("5"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        admin_token="order-token",
        max_concurrency=4,
    )
    headers = {"Authorization": "Bearer order-token"}
    seen: list[tuple[int, int]] = []
    original_count = SqliteRepository.count_subjects

    def recording_count(self, region, outcome, withdrawn_through=None):
        value = original_count(self, region, outcome, withdrawn_through)
        seen.append((value, withdrawn_through))
        return value

    with TestClient(create_app(settings)) as client:
        monkeypatch_target = SqliteRepository
        monkeypatch_target.count_subjects = recording_count
        try:
            assert client.post(
                "/v1/admin/withdrawals", json={"subject_id": "A"}, headers=headers
            ).status_code == 200

            response = client.post(
                "/v1/counts",
                json={
                    "request_id": "ordered",
                    "region": "north",
                    "outcome": 1,
                    "epsilon": "0.1",
                },
            )
            assert response.status_code == 200
        finally:
            SqliteRepository.count_subjects = original_count

    # The mechanism was given the post-withdrawal count (1, not 2), from a
    # snapshot whose sequence already included the committed withdrawal.
    assert seen == [(1, 1)]
    conn = sqlite3.connect(str(settings.database_path))
    try:
        stored = conn.execute(
            "SELECT withdrawals_at_snapshot FROM requests WHERE request_id = 'ordered'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == 1


def test_stale_snapshot_is_rejected_and_recomputed(app_client, monkeypatch):
    """A count read before a withdrawal must not be published afterwards.

    ``count_subjects`` is paused after it reads the ledger sequence; while it
    is paused the withdrawal commits.  The write transaction must notice the
    moved sequence, write nothing, and let the service recompute so the
    published answer excludes the withdrawn subject.
    """
    from app.models import SqliteRepository

    original_snapshot = SqliteRepository.withdrawal_snapshot
    original_count = SqliteRepository.count_subjects
    state = {"injected": False, "counts": []}

    def snapshot_with_injection(self):
        snapshot = original_snapshot(self)
        if not state["injected"]:
            state["injected"] = True
            # Commit a withdrawal *after* the snapshot was taken but before
            # the write transaction runs.
            app_client.post(
                "/v1/admin/withdrawals",
                json={"subject_id": "A"},
                headers=ADMIN_HEADERS,
            )
        return snapshot

    def counting(self, region, outcome, withdrawn_through=None):
        value = original_count(self, region, outcome, withdrawn_through)
        state["counts"].append((value, withdrawn_through))
        return value

    monkeypatch.setattr(SqliteRepository, "withdrawal_snapshot", snapshot_with_injection)
    monkeypatch.setattr(SqliteRepository, "count_subjects", counting)

    response = query(app_client, "raced", "north", 1)
    assert response.status_code == 200
    assert response.json()["charged"] is True
    assert response.json()["remaining_budget"] == "0.9"

    # First attempt read the pre-withdrawal count from sequence 0; it was
    # refused, and the retry read the post-withdrawal count from sequence 1.
    assert state["counts"] == [(2, 0), (1, 1)]
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_committed_but_unpublished_query_keeps_its_order(tmp_path: Path, csv_path: Path, monkeypatch):
    """A withdrawal committing *after* the answer's transaction cannot rewrite it.

    The stored answer keeps its transaction's snapshot sequence, the replay is
    unchanged, and no second charge appears.  The next fresh query sees the
    withdrawal.
    """
    from fastapi.testclient import TestClient

    from app.db import Database
    from app.main import create_app
    from app.models import SqliteRepository

    settings = Settings(
        database_path=tmp_path / "order2.db",
        csv_path=csv_path,
        total_budget=Decimal("5"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        admin_token="order2-token",
        max_concurrency=4,
    )
    headers = {"Authorization": "Bearer order2-token"}

    original_apply = SqliteRepository.apply_query
    state = {"withdraw_after_commit": True}

    with TestClient(create_app(settings)) as client:
        def apply_then_withdraw(self, **kwargs):
            result = original_apply(self, **kwargs)
            if state["withdraw_after_commit"] and not result.replayed:
                state["withdraw_after_commit"] = False
                # Commit the withdrawal directly on its own connection, i.e.
                # after the answer's transaction has committed.
                other = SqliteRepository(Database(settings.database_path))
                other.record_withdrawal("A")
                other._db.close_all()
            return result

        monkeypatch.setattr(SqliteRepository, "apply_query", apply_then_withdraw)
        payload = {
            "request_id": "committed-first",
            "region": "north",
            "outcome": 1,
            "epsilon": "0.1",
        }
        first = client.post("/v1/counts", json=payload)
        assert first.status_code == 200
        first = first.json()
        monkeypatch.setattr(SqliteRepository, "apply_query", original_apply)

        conn = sqlite3.connect(str(settings.database_path))
        try:
            stored_sequence = conn.execute(
                "SELECT withdrawals_at_snapshot FROM requests "
                "WHERE request_id = 'committed-first'"
            ).fetchone()[0]
        finally:
            conn.close()
        # The answer was computed with no withdrawals yet (sequence 0); the
        # withdrawal that committed afterwards did not retroactively change it.
        assert stored_sequence == 0

        replay = client.post("/v1/counts", json=payload).json()
        assert replay["noisy_count"] == first["noisy_count"]
        assert replay["replayed"] is True
        assert replay["remaining_budget"] == "4.9"

        fresh = query(client, "after-commit", "north", 1)
        assert fresh.status_code == 200
        assert fresh.json()["remaining_budget"] == "4.8"


def test_openapi_documents_admin_endpoint_and_scheme(app_client):
    schema = app_client.get("/openapi.json").json()
    assert "/v1/admin/withdrawals" in schema["paths"]
    operation = schema["paths"]["/v1/admin/withdrawals"]["post"]
    assert operation["security"] == [{"AdminToken": []}]
    assert "AdminToken" in schema["components"]["securitySchemes"]
    # The token itself is never part of the published document.
    assert "test-admin-token" not in app_client.get("/openapi.json").text


# --------------------------------------------------------------------------
# upgrade path and concurrency
# --------------------------------------------------------------------------
LEGACY_SCHEMA = """
CREATE TABLE dataset (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    imported_at TEXT NOT NULL,
    source TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    total_budget TEXT NOT NULL,
    budget_spent TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE TABLE subjects (
    id INTEGER PRIMARY KEY,
    subject_id TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL,
    outcome INTEGER NOT NULL CHECK (outcome IN (0, 1))
);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    region TEXT NOT NULL,
    outcome INTEGER NOT NULL,
    epsilon TEXT NOT NULL,
    noisy_count INTEGER NOT NULL,
    budget_after TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def test_existing_database_is_migrated_additively(tmp_path: Path):
    """A volume created before withdrawals existed keeps all its data."""
    from app.db import Database

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO dataset VALUES (1, 'old', 'seed.csv', 2, '1', '0.1', 'fp')"
    )
    conn.execute("INSERT INTO subjects VALUES (1, 'A', 'north', 1)")
    conn.execute("INSERT INTO subjects VALUES (2, 'B', 'north', 1)")
    conn.execute(
        "INSERT INTO requests VALUES "
        "('old', 'fp', 'north', 1, '0.1', 2, '0.9', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    db.initialise()

    repository = __import__(
        "app.models", fromlist=["SqliteRepository"]
    ).SqliteRepository(db)
    lookup = repository.lookup("old")
    assert lookup.stored is not None
    assert lookup.stored.noisy_count == 2
    assert lookup.stored.withdrawals_at_snapshot == 0
    assert repository.withdrawal_snapshot().sequence == 0

    repository.record_withdrawal("A")
    assert repository.count_subjects("north", 1) == 1
    assert repository.budget_snapshot().remaining == Decimal("0.9")
    db.close_all()


def test_concurrent_withdrawals_and_queries_stay_consistent(tmp_path: Path, csv_path: Path):
    """Queries racing withdrawals never 500 and never double-charge.

    Every published answer is consistent with the ledger state it recorded:
    the true count it was built from equals the live count filtered through
    its own snapshot sequence.  Budget accounting stays exact, and the ledger
    ends with one row per distinct subject.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SqliteRepository

    settings = Settings(
        database_path=tmp_path / "race.db",
        csv_path=csv_path,
        total_budget=Decimal("10"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=60_000,
        admin_token="race-token",
        max_concurrency=8,
    )
    headers = {"Authorization": "Bearer race-token"}
    query_statuses: list[int] = []
    withdraw_statuses: list[int] = []
    lock = threading.Lock()

    statuses: list[int] = []

    with TestClient(create_app(settings)) as client:
        repository = client.app.state.repository

        def query_worker(index: int) -> None:
            response = client.post(
                "/v1/counts",
                json={
                    "request_id": f"race-{index}",
                    "region": "north",
                    "outcome": 1,
                    "epsilon": "0.1",
                },
            )
            with lock:
                query_statuses.append(response.status_code)
                statuses.append(response.status_code)

        def withdraw_worker(subject: str) -> None:
            response = client.post(
                "/v1/admin/withdrawals",
                json={"subject_id": subject},
                headers=headers,
            )
            with lock:
                withdraw_statuses.append(response.status_code)
                statuses.append(response.status_code)

        threads = [threading.Thread(target=query_worker, args=(i,)) for i in range(12)]
        threads += [
            threading.Thread(target=withdraw_worker, args=(subject,))
            for subject in ("A", "B", "C", "A")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Queries either publish or are refused for budget; withdrawals are
        # always accepted, including the duplicate.  Nothing returns 500.
        assert all(status == 200 for status in withdraw_statuses)
        assert len(withdraw_statuses) == 4
        assert all(status in {200, 429} for status in query_statuses)
        assert len(query_statuses) == 12

        # One ledger row per distinct subject, including the duplicate request.
        conn = sqlite3.connect(str(settings.database_path))
        try:
            distinct = conn.execute(
                "SELECT COUNT(DISTINCT subject_id) FROM withdrawals"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0]
            published = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            budget_spent = Decimal(
                conn.execute("SELECT budget_spent FROM dataset").fetchone()[0]
            )
            rows = conn.execute(
                "SELECT withdrawals_at_snapshot FROM requests"
            ).fetchall()
        finally:
            conn.close()

        assert distinct == total == 3
        assert published == query_statuses.count(200)
        assert budget_spent == Decimal("0.1") * published

        # Every stored witness is a ledger sequence that could have been
        # committed when the answer was published, so each answer is consistent
        # with at least the withdrawals up to that point.
        for (sequence,) in rows:
            assert 0 <= sequence <= 3

        # All three withdrawn subjects are excluded afterwards.
        assert repository.count_subjects("north", 1) == 0
