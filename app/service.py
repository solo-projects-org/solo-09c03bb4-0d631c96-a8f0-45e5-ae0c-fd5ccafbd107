"""Query orchestration.

Properties enforced here rather than in the HTTP layer:

* A request id is bound to the exact parameter set.  Replaying the same id
  with the same parameters returns the stored answer and spends nothing;
  replaying it with different parameters is rejected.
* The noisy value is drawn *before* entering the write transaction, but it is
  committed together with the budget deduction and the stored answer.  If the
  save fails the transaction rolls back: no budget is spent and no answer
  exists, so the client may retry.
* If the commit succeeds but the HTTP response is lost, the stored answer is
  durable and a retry after a restart returns the same number.
* Withdrawn subjects are excluded from every count.  The true count is taken
  from a snapshot tagged with the withdrawal ledger's sequence; the write
  transaction re-reads that sequence and refuses to publish a count that
  predates a withdrawal which has already committed.  A refusal writes
  nothing, so the service simply takes a fresh snapshot and draws again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from .errors import RequestIdConflict
from .models import ReleaseResult, Repository
from .privacy import canonical_epsilon, make_noisy_count, request_fingerprint

logger = logging.getLogger(__name__)

#: Safety bound on withdraw/recompute cycles.  Each retry only happens after a
#: different withdrawal committed, and a withdrawal is a rare administrative
#: action; hitting this bound means the ledger is churning faster than queries
#: can complete, in which case refusing is preferable to publishing an answer
#: from a stale snapshot.
MAX_SNAPSHOT_ATTEMPTS = 8


@dataclass(frozen=True)
class QueryOutcome:
    noisy_count: int
    remaining_budget: Decimal
    epsilon: str
    replayed: bool

    @property
    def charged(self) -> bool:
        return not self.replayed


class SnapshotUnstable(Exception):
    """Withdrawals kept committing; no answer could be published in order."""


class QueryService:
    def __init__(self, repository: Repository, total_budget: Decimal) -> None:
        self._repo = repository
        self._total_budget = total_budget

    def budget_snapshot(self):
        return self._repo.budget_snapshot()

    def query(
        self, *, request_id: str, region: str, outcome: int, epsilon: Decimal
    ) -> QueryOutcome:
        fingerprint = request_fingerprint(region, outcome, epsilon)

        # Fast path: an existing answer is a pure read.  It does not consult
        # the budget, so a replay still works after the budget is exhausted.
        # The reported remaining budget is the *current* global figure.  A
        # replay also ignores withdrawals: published answers are immutable.
        lookup = self._repo.lookup(request_id)
        stored = lookup.stored
        if stored is not None:
            if stored.fingerprint != fingerprint:
                raise RequestIdConflict(request_id)
            return QueryOutcome(
                noisy_count=stored.noisy_count,
                remaining_budget=lookup.remaining_budget,
                epsilon=stored.epsilon,
                replayed=True,
            )

        for attempt in range(1, MAX_SNAPSHOT_ATTEMPTS + 1):
            snapshot = self._repo.withdrawal_snapshot()
            true_count = self._repo.count_subjects(
                region, outcome, snapshot.sequence
            )
            released = make_noisy_count(true_count, epsilon)

            result: ReleaseResult = self._repo.apply_query(
                request_id=request_id,
                fingerprint=fingerprint,
                region=region,
                outcome=outcome,
                epsilon=epsilon,
                noisy_count=released,
                withdrawn_through=snapshot.sequence,
            )
            if not result.stale_snapshot:
                break
            logger.info(
                "withdrawal committed after snapshot for %s; recomputing "
                "(attempt %d)",
                request_id,
                attempt,
            )
        else:
            # Exhausted the retry budget.  Nothing was written by any refused
            # attempt, so no budget was charged and no answer exists.
            raise SnapshotUnstable(
                "withdrawals are being recorded too quickly to publish an "
                "answer from a stable snapshot"
            )

        if result.replayed:
            # Another request handler committed this id first.  Its answer is
            # the one that was published; ours was never released, so the
            # budget charged for ours is rolled back with our transaction.
            logger.info("request %s committed concurrently; replaying", request_id)

        return QueryOutcome(
            noisy_count=result.noisy_count,
            remaining_budget=result.remaining_budget,
            epsilon=canonical_epsilon(epsilon),
            replayed=result.replayed,
        )

    # -- administrative ----------------------------------------------------
    def withdraw_subject(self, subject_id: str):
        return self._repo.record_withdrawal(subject_id)


__all__ = ["QueryService", "QueryOutcome", "SnapshotUnstable"]
