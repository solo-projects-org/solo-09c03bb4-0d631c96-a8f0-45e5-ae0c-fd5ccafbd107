# Follow-up Count Privacy API

A small statistical API that hands collaborating research groups **noisy**
follow-up counts, never exact ones. Subject-level data is imported once from
a CSV and is never replaced afterwards. Each query for a
(`region`, `outcome`) cell answers with a count perturbed by Laplace noise of
scale `1/ε`, and charges `ε` against a single global privacy budget that
starts at `1`.

Subjects who withdraw consent are excluded from every **future** count through
an administrator-only endpoint. The withdrawal is an append to a ledger — the
imported data, already-released answers, request replays and the spent budget
are never rewritten.

- **Stack:** FastAPI · SQLite · Uvicorn
- **Docs:** interactive Swagger UI at `/docs`, OpenAPI JSON at `/openapi.json`
- **Guarantee:** ε-differential privacy per release (histogram query,
  sensitivity 1), with lifetime budget accounting

---

## Quick start

```bash
export ADMIN_TOKEN=$(openssl rand -hex 32)
docker compose up --build
```

Then open <http://localhost:8000/docs>.

`ADMIN_TOKEN` is optional so the service always starts; without it the
withdrawal endpoint answers `403 admin_disabled` instead of accepting a
guessable default. Set it before starting if withdrawal requests are expected.

The bundled sample data set is `seed/followup.csv` (100 subjects across
`north`, `south`, `east`). It is imported **once**, on the very first start.
A named volume (`followup-data`) holds the database, so restarts keep the
imported data, the spent budget and every released answer.

Tear down completely, including the volume:

```bash
docker compose down -v
```

### Running without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --port 8000
```

---

## API

### `POST /v1/counts`

```bash
curl -s -X POST http://localhost:8000/v1/counts \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-0001","region":"north","outcome":1,"epsilon":"0.1"}'
```

```json
{
  "request_id": "req-0001",
  "region": "north",
  "outcome": 1,
  "epsilon": "0.1",
  "noisy_count": 27,
  "remaining_budget": "0.9",
  "replayed": false,
  "charged": true
}
```

| Field | Meaning |
| --- | --- |
| `noisy_count` | Integer count after adding Laplace(0, 1/ε) noise, clamped at 0. Never the exact count. |
| `remaining_budget` | Global budget left **after** this call, as an exact decimal string. |
| `replayed` | `true` when a stored answer was returned instead of a new one. |
| `charged` | `true` when this call spent budget. |

`request_id` is a client-generated idempotency key. Retrying it with the same
parameters returns the identical stored answer, `replayed: true`, and no new
charge. Reusing it with any different parameter (region, outcome or epsilon)
is rejected with `409 request_id_conflict`.

### `GET /v1/budget`

```json
{"total_budget": "1", "remaining_budget": "0.9"}
```

### `GET /v1/requests/{request_id}`

Returns the stored answer for a request id (same body as above) or `404`.

### `POST /v1/admin/withdrawals`

Administrator-only. Records that a subject withdrew consent so that the
subject is excluded from all subsequently computed counts.

```bash
curl -s -X POST http://localhost:8000/v1/admin/withdrawals \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"subject_id":"S0001"}'
```

```json
{"status": "accepted"}
```

* **Uniform receipt.** The body is exactly `{"status":"accepted"}` for *every*
  accepted request — whether the subject exists, was already withdrawn, or was
  never in the imported CSV. The subject id is not echoed and no sequence,
  timestamp or "changed" flag is returned, so the endpoint cannot be used to
  probe which subjects are in the data set.
* **Append-only.** One ledger row per subject; repeats change nothing. The only
  place a subject identifier is stored outside the one-shot import.
* **What is untouched.** Imported rows, stored answers, request-id replays and
  `budget_spent` are left exactly as they were. A withdrawal is not a release
  and spends no privacy budget.
* **Ordering.** The withdrawal takes effect for any answer whose snapshot is
  taken after it commits; an answer whose snapshot predates it but is still in
  flight is refused and recomputed, so it cannot carry the pre-withdrawal count.
  See [Ordering withdrawals against queries](#ordering-withdrawals-against-queries).
* **Auth.** Missing, malformed or wrong bearer token → `401 unauthorized`.
  Token unset server-side → `403 admin_disabled`. No database work happens
  before the check succeeds, so a rejected request changes no table.
* **Failure.** A write failure rolls the transaction back and returns `500`;
  the ledger and its counter are unchanged.

### `GET /health`

Liveness plus whether the data set has been loaded and its row count.

### Errors

All errors share one shape:

```json
{"error": "invalid_request", "message": "request validation failed", "details": [...]}
```

| Status | `error` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing or wrong administrator bearer token |
| 403 | `admin_disabled` | `ADMIN_TOKEN` is not configured |
| 409 | `request_id_conflict` | Same `request_id`, different parameters |
| 422 | `invalid_request` | Malformed body, `ε` outside `(0, 1]`, outcome not `0`/`1`, … |
| 429 | `budget_exhausted` | `ε` does not fit in the remaining global budget |
| 503 | `dataset_missing` | No data set imported |
| 503 | `snapshot_unstable` | Withdrawals keep committing; retry shortly |
| 500 | `internal_error` | Unexpected failure; nothing was charged or changed |

`POST /v1/admin/withdrawals` has its own Swagger operation with the full list
of status codes; the other endpoints are unchanged.

---

## Privacy model

- **Mechanism.** The count of subjects in one cell has global sensitivity 1
  (adding or removing one person changes it by at most one). Adding a sample
  from `Laplace(0, 1/ε)` therefore gives ε-differential privacy for that
  answer. Rounding to an integer and clamping at zero are post-processing and
  preserve the guarantee.
- **Composition.** Every release is charged `ε` against one global budget of
  1, so a sequence of releases is safe under basic sequential composition
  while the sum of ε stays within the budget. Once the budget is exhausted,
  only replays of already-released answers are served, free of charge.
- **No exact values.** The exact counts are computed inside the process but
  are never returned, logged or included in an error message. Sweep
  `region` × `outcome` and intersect only through noisy releases, each paid
  for by the budget.

---

## Correctness properties and how they are enforced

| Requirement | Enforcement |
| --- | --- |
| CSV imported once, never replaced | The importer short-circuits whenever the singleton `dataset` row exists; the check is repeated under the write lock. A different CSV is ignored. |
| Unique subject id | `UNIQUE` constraint plus a pre-insert duplicate scan; a bad file leaves zero rows and no data-set row behind (whole import is one transaction). |
| 0/1 outcomes only | CSV validation and a `CHECK` constraint on `subjects.outcome`. |
| Laplace noise, scale `1/ε` | `app/privacy.py`; draws uniforms from the OS CSPRNG in the open interval `(0, 1)`. |
| Budget never overspent under concurrency | Budget check and deduction happen in one `BEGIN IMMEDIATE` transaction; SQLite allows exactly one writer, so the read-modify-write is serialised. Exact decimal arithmetic — never floats — so `1 - 10×0.1` is exactly `0`. |
| Same id + same parameters → same answer, no charge | The stored `noisy_count` is returned; the fingerprint of `(region, outcome, canonical ε)` is compared first. |
| Same id + different parameters → reject | Fingerprint mismatch raises `409`. |
| Invalid request never charges | Validation happens before the service is called; no row is written. |
| Save failure never charges, and produces no answer | The noisy draw, the request row and the budget update are one transaction. Any exception rolls back both; `GET /v1/budget` is unchanged. |
| Commit succeeded but response lost | The answer is durable before the response is written. A retry — even after a restart — returns the identical stored value. |
| Withdrawal is invisible on the wire | Every accepted request returns the constant `{"status":"accepted"}`; unknown, already-withdrawn and fresh ids are indistinguishable. |
| Withdrawal never rewrites history | The ledger is append-only; `subjects`, `requests` and `dataset.budget_spent` are never updated by a withdrawal. |
| Withdrawal covers all later answers | `count_subjects` excludes every ledger row up to the query's snapshot sequence. |
| No answer published from a stale snapshot | A new `requests` row records the ledger sequence it was computed from; the write transaction re-reads the counter and, if it moved, returns `stale_snapshot` **without writing or charging**, and the service recomputes on a fresh snapshot. `BEGIN IMMEDIATE` means no withdrawal can commit while the answer commits. |
| Withdrawal survives restart | The ledger is a normal table in the same WAL database on the volume; the CSV is still imported only once, so a restart reuses the ledger as-is. |
| Auth/write failure changes no data | Authentication is checked before any connection is used; the ledger insert and counter bump share one transaction that rolls back on any error. |

---

## Ordering withdrawals against queries

Both orderings are handled, and neither rewrites a published answer:

* **Withdrawal committed before the query is published.** The query's true
  count was read together with a ledger sequence `s`. Before inserting the
  answer, the write transaction compares `s` to the counter. If a withdrawal
  committed in between, the counter has moved: the transaction writes nothing
  (no stored answer, no budget charge) and the service re-reads a fresh
  snapshot, recomputes and draws new noise. The answer that is eventually
  published therefore reflects every withdrawal committed before it.
* **Withdrawal committed after the query is published.** The answer is already
  durable and earlier by commit order; it keeps its count, its stored value,
  its replay and its budget charge. It records `withdrawals_at_snapshot = s`,
  and only future answers exclude the subject.

Released answers are immutable by design: neither a withdrawal nor a replay can
change a number that has already left the process.

---

## Concurrency test

The suite spins up 20 simultaneous `ε = 0.1` requests against a budget of
`1.0` and asserts exactly ten `200`s, ten `429`s and a final budget of `0`
(`tests/test_api.py::test_concurrent_requests_never_overspend`).

A second test races 12 queries against 4 withdrawals (including a duplicate)
and checks that every query either publishes with exact budget accounting or
is refused for budget, none fails with `500`, each stored answer records a
legitimate ledger sequence, and the ledger ends with one row per distinct
subject. The ordering tests inject a withdrawal between the snapshot and the
commit (and after the commit) to prove the stale snapshot is refused and the
committed answer is preserved.

```bash
pytest -q
```

---

## Layout

```
app/
  config.py     env-driven settings (paths, budget, admin token)
  db.py         SQLite connections, schema, additive migrations, transactions
  importer.py   validated one-shot CSV import
  privacy.py    Laplace mechanism, canonical decimals, request fingerprints
  models.py     dataclasses, budget critical section, withdrawal ledger
  service.py    query orchestration, idempotency, snapshot/recompute loop
  main.py       FastAPI app, endpoints, admin auth, error mapping
examples/followup.csv   sample input (same as seed/followup.csv)
seed/followup.csv       data set imported by the container on first start
tests/                  pytest suite (API, privacy, concurrency, durability)
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `data/followup.db` | SQLite file (mounted volume in Docker) |
| `CSV_PATH` | `seed/followup.csv` | CSV imported on first start only |
| `TOTAL_BUDGET` | `1` | Lifetime privacy budget |
| `IMPORT_ON_STARTUP` | `1` | Set `0` to boot without an import attempt |
| `SQLITE_BUSY_TIMEOUT_MS` | `30000` | How long writers wait for the lock |
| `ADMIN_TOKEN` | *(unset)* | Bearer token for `POST /v1/admin/withdrawals`. No default: unset disables the endpoint (`403 admin_disabled`). |
| `API_PORT` | `8000` | Host port published by Compose |

> **Token handling:** `ADMIN_TOKEN` is read from the environment at start-up
> and compared in constant time. It is never written to SQLite, logged, placed
> in an error message or included in the OpenAPI document.

> **Operational note:** every successful new answer consumes budget
> permanently. To start a fresh study, remove the volume
> (`docker compose down -v`) and start again — that is the only way the data
> set or the budget is ever reset, by design.
