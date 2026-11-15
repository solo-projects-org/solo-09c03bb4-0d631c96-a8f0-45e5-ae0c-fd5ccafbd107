"""Public data structures and the SQL-backed repository.

All budget quantities are decimals and are stored as canonical decimal
strings, never as SQLite REAL values, so ``1 - 0.1`` stays exactly ``0.9``.

Withdrawal bookkeeping
----------------------
Withdrawing a subject only *appends* to the ``withdrawals`` ledger; it never
rewrites the imported ``subjects`` table or any released answer.  Counts are
filtered against the ledger at query time, so a withdrawal takes effect for
every answer whose snapshot is taken after the withdrawal committed.

Commit ordering is enforced with two witnesses:

* ``withdrawal_counter.last_sequence`` is bumped inside the same write
  transaction that inserts a ledger row.
* Every new ``requests`` row records the counter value observed by its
  snapshot (``withdrawals_at_snapshot``).

A query computes its count from a snapshot carrying sequence ``s`` and then
enters the serialised write transaction.  If the counter has moved past ``s``,
a withdrawal committed after the count was read; the transaction writes
nothing and reports ``stale_snapshot`` so the service re-reads and re-noises.
Because ``BEGIN IMMEDIATE`` serialises writers, no withdrawal can commit while
the answer row and budget update are being written, so the published answer
always reflects at least every withdrawal committed before it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from .privacy import canonical_decimal


@dataclass(frozen=True)
class StoredRequest:
    request_id: str
    fingerprint: str
    region: str
    outcome: int
    epsilon: str
    noisy_count: int
    budget_after: str
    created_at: str
    withdrawals_at_snapshot: int


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of a query: a fresh release, or a replay of a stored one.

    ``stale_snapshot`` is set when the write transaction refused to publish an
    answer whose count predates a withdrawal that has already committed.  The
    transaction wrote nothing (no charge, no stored answer); the caller must
    recompute from a fresh snapshot.
    """

    noisy_count: int
    remaining_budget: Decimal
    replayed: bool
    stale_snapshot: bool = False


@dataclass(frozen=True)
class BudgetSnapshot:
    total: Decimal
    spent: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.total - self.spent


@dataclass(frozen=True)
class LookupResult:
    """A stored answer (if any) plus the *current* global remaining budget."""

    stored: StoredRequest | None
    remaining_budget: Decimal


@dataclass(frozen=True)
class WithdrawalSnapshot:
    """Ledger state used to compute one answer.

    ``sequence`` is the serialisation witness and ``withdrawn_count`` the
    number of ledger rows visible at that same snapshot.
    """

    sequence: int
    withdrawn_count: int


@dataclass(frozen=True)
class WithdrawalReceipt:
    """Result of recording a withdrawal (never exposed on the wire)."""

    subject_id: str
    sequence: int
    newly_recorded: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository(Protocol):
    """Storage interface used by the query service."""

    def budget_snapshot(self) -> BudgetSnapshot: ...

    def lookup(self, request_id: str) -> LookupResult: ...

    def withdrawal_snapshot(self) -> WithdrawalSnapshot: ...

    def count_subjects(
        self, region: str, outcome: int, withdrawn_through: int | None = None
    ) -> int: ...

    def apply_query(
        self,
        *,
        request_id: str,
        fingerprint: str,
        region: str,
        outcome: int,
        epsilon: Decimal,
        noisy_count: int,
        withdrawn_through: int | None = None,
    ) -> ReleaseResult: ...

    def record_withdrawal(self, subject_id: str) -> WithdrawalReceipt: ...


class SqliteRepository:
    """Repository whose mutations run inside one serialised transaction."""

    def __init__(self, database) -> None:  # noqa: ANN001 - avoids an import cycle
        self._db = database

    # -- reads -------------------------------------------------------------
    def budget_snapshot(self) -> BudgetSnapshot:
        with self._db.read_transaction() as conn:
            return _budget_snapshot(conn)

    def lookup(self, request_id: str) -> LookupResult:
        """Read the stored answer and the current remaining budget atomically."""
        with self._db.read_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            remaining = _budget_snapshot(conn).remaining
            return LookupResult(
                stored=_row_to_request(row) if row else None,
                remaining_budget=remaining,
            )

    def withdrawal_snapshot(self) -> WithdrawalSnapshot:
        """Ledger state as of one consistent read snapshot."""
        with self._db.read_transaction() as conn:
            return _withdrawal_snapshot(conn)

    def count_subjects(
        self, region: str, outcome: int, withdrawn_through: int | None = None
    ) -> int:
        """Live subject count for a cell.

        Withdrawn subjects are excluded; ``withdrawn_through`` restricts the
        exclusion to ledger rows whose sequence is at most that value, so the
        count matches the snapshot the caller will later validate against.
        ``None`` excludes every withdrawal committed at count time.
        """
        with self._db.read_transaction() as conn:
            if withdrawn_through is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM subjects
                    WHERE region = ? AND outcome = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM withdrawals w
                          WHERE w.subject_id = subjects.subject_id
                      )
                    """,
                    (region, int(outcome)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM subjects
                    WHERE region = ? AND outcome = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM withdrawals w
                          WHERE w.subject_id = subjects.subject_id
                            AND w.sequence <= ?
                      )
                    """,
                    (region, int(outcome), int(withdrawn_through)),
                ).fetchone()
            return int(row["n"])

    # -- the critical section ---------------------------------------------
    def apply_query(
        self,
        *,
        request_id: str,
        fingerprint: str,
        region: str,
        outcome: int,
        epsilon: Decimal,
        noisy_count: int,
        withdrawn_through: int | None = None,
    ) -> ReleaseResult:
        """Atomically consume budget and persist the answer.

        ``noisy_count`` is ignored when a stored answer for ``request_id``
        already exists: a replay always returns the originally released
        value and never charges budget.  A replay is also exempt from the
        snapshot check, because the published answer is immutable.

        When ``withdrawn_through`` is given, the transaction refuses to publish
        a count computed from an older ledger state: if any withdrawal has
        committed since, it returns ``stale_snapshot`` without writing or
        charging.  If a withdrawal commits after this transaction, the answer
        is already durable and is the earlier release by commit order.
        """
        from .errors import BudgetExhausted, RequestIdConflict

        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                stored = _row_to_request(row)
                if stored.fingerprint != fingerprint:
                    raise RequestIdConflict(request_id)
                return ReleaseResult(
                    noisy_count=stored.noisy_count,
                    remaining_budget=_budget_snapshot(conn).remaining,
                    replayed=True,
                )

            if withdrawn_through is not None:
                current_sequence = _withdrawal_sequence(conn)
                if current_sequence != int(withdrawn_through):
                    # A withdrawal committed between our count snapshot and
                    # this transaction.  Nothing is written: no budget is
                    # charged and no answer is published from the stale view.
                    return ReleaseResult(
                        noisy_count=int(noisy_count),
                        remaining_budget=_budget_snapshot(conn).remaining,
                        replayed=False,
                        stale_snapshot=True,
                    )

            snapshot = _budget_snapshot(conn)
            if epsilon > snapshot.remaining:
                raise BudgetExhausted(epsilon, snapshot.remaining, snapshot.total)
            budget_after = snapshot.remaining - epsilon
            new_spent = snapshot.total - budget_after
            withdrawals_at_snapshot = _withdrawal_sequence(conn)

            # The request row and the budget update commit together.  Any
            # failure while writing raises out of the transaction, which
            # rolls both back: no charge and no stored answer.
            conn.execute(
                """
                INSERT INTO requests
                    (request_id, request_hash, region, outcome, epsilon,
                     noisy_count, budget_after, created_at,
                     withdrawals_at_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    fingerprint,
                    region,
                    int(outcome),
                    canonical_decimal(epsilon),
                    int(noisy_count),
                    canonical_decimal(budget_after),
                    _now(),
                    withdrawals_at_snapshot,
                ),
            )
            conn.execute(
                "UPDATE dataset SET budget_spent = ? WHERE id = 1",
                (canonical_decimal(new_spent),),
            )
            return ReleaseResult(
                noisy_count=int(noisy_count),
                remaining_budget=budget_after,
                replayed=False,
            )

    # -- withdrawal ledger -------------------------------------------------
    def record_withdrawal(self, subject_id: str) -> WithdrawalReceipt:
        """Append a withdrawal for ``subject_id``, once.

        Duplicates are swallowed by the ledger's primary key and change
        nothing, so the caller can return the same receipt whether the subject
        exists, was already withdrawn or has never been imported.  Only the
        ledger is written; imported rows and released answers are untouched.
        """
        with self._db.write_transaction() as conn:
            existing = conn.execute(
                "SELECT sequence FROM withdrawals WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            if existing is not None:
                return WithdrawalReceipt(
                    subject_id=subject_id,
                    sequence=int(existing["sequence"]),
                    newly_recorded=False,
                )

            sequence = _next_withdrawal_sequence(conn)
            conn.execute(
                "INSERT INTO withdrawals (subject_id, withdrawn_at, sequence) "
                "VALUES (?, ?, ?)",
                (subject_id, _now(), sequence),
            )
            # Bumped in the same transaction: any query that starts after this
            # commit sees the new sequence and therefore the new ledger row.
            conn.execute(
                "UPDATE withdrawal_counter SET last_sequence = ? WHERE id = 1",
                (sequence,),
            )
            return WithdrawalReceipt(
                subject_id=subject_id,
                sequence=sequence,
                newly_recorded=True,
            )


def _budget_snapshot(conn: sqlite3.Connection) -> BudgetSnapshot:
    from .errors import DatasetMissing

    row = conn.execute(
        "SELECT total_budget, budget_spent FROM dataset WHERE id = 1"
    ).fetchone()
    if row is None:
        raise DatasetMissing("no dataset imported")
    return BudgetSnapshot(
        total=Decimal(str(row["total_budget"])),
        spent=Decimal(str(row["budget_spent"])),
    )


def _withdrawal_sequence(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_sequence FROM withdrawal_counter WHERE id = 1"
    ).fetchone()
    return int(row["last_sequence"]) if row is not None else 0


def _next_withdrawal_sequence(conn: sqlite3.Connection) -> int:
    """Allocate the next ledger sequence inside an exclusive transaction.

    The counter is authoritative and bumped with every insert; the ``MAX``
    guard is defensive only, so a counter left behind by an interrupted
    upgrade can never cause a sequence to be reused.
    """
    row = conn.execute("SELECT MAX(sequence) AS m FROM withdrawals").fetchone()
    highest = int(row["m"]) if row is not None and row["m"] is not None else 0
    return max(_withdrawal_sequence(conn), highest) + 1


def _withdrawal_snapshot(conn: sqlite3.Connection) -> WithdrawalSnapshot:
    row = conn.execute("SELECT COUNT(*) AS n FROM withdrawals").fetchone()
    return WithdrawalSnapshot(
        sequence=_withdrawal_sequence(conn),
        withdrawn_count=int(row["n"]),
    )


def _row_to_request(row: sqlite3.Row) -> StoredRequest:
    return StoredRequest(
        request_id=str(row["request_id"]),
        fingerprint=str(row["request_hash"]),
        region=str(row["region"]),
        outcome=int(row["outcome"]),
        epsilon=str(row["epsilon"]),
        noisy_count=int(row["noisy_count"]),
        budget_after=str(row["budget_after"]),
        created_at=str(row["created_at"]),
        withdrawals_at_snapshot=int(row["withdrawals_at_snapshot"]),
    )
