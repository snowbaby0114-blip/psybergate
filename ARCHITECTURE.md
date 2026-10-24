# Architecture

```
 Browser ──► nginx (web, :8080)  ── serves the Angular SPA, proxies /api/ ──┐
                                                                            ▼
                         FastAPI (api, :8000)  routers ─► services ─► repositories ─► PostgreSQL 16
                                                            │
                                                            └─► app/domain  (pure rule engine, no I/O)
```

| Part | Where | Responsibility |
|---|---|---|
| Web | `frontend/` | Angular 21 SPA, four views. The URL holds search/ledger state; one cancellable request pipeline per view; typed API layer with a single `ApiError`. |
| Routers | `backend/app/api/v1/` | Parse and validate HTTP, call one service function, shape the response. No business logic, no commits. |
| Services | `backend/app/services/` | Own the transaction: take the locks, load data, call the engine, write the outcome. `dispenses.py` is the write path, `formulary.py` supersedes rules. |
| **Rule evaluation** | **`backend/app/domain/`** | **Pure functions on plain dataclasses, standard library only (a test enforces that).** `dispense.py` evaluates a dispense and returns *every* violation; `rules.py` finds the rule in force and plans a supersede; `timeutil.py` is the only place UTC instants and Africa/Johannesburg business dates are converted; `idempotency.py` fingerprints a request. |
| Repositories | `backend/app/repositories.py` | All SQL. Query builders return `Select` objects so tests can `EXPLAIN` exactly what production runs. |
| Errors | `backend/app/errors.py` | One problem+json shape (RFC 9457 plus a `violations` list) for every failure, whichever handler produced it. |
| Schema | `backend/app/models.py`, `backend/alembic/` | Migrations are the only thing that creates the schema; a test proves models and migrations describe the same database. |
| Seed, bench | `backend/app/seed.py`, `backend/app/bench.py` | Deterministic data generator that admits every row through the real engine; HTTP-only latency benchmark. |

## A dispense, end to end (`POST /api/v1/dispenses`)

One transaction, READ COMMITTED, locks always taken in this order:

1. `lock_timeout` set; **advisory lock on the idempotency key**; look the key up in `dispense_attempts` — a hit replays the original outcome (or fails if the payload differs).
2. **`SELECT … FOR SHARE` on the medicine row** — dispenses of one medicine run side by side; a rule change takes `FOR UPDATE` on the same row and so waits for them.
3. **Advisory lock on (medicine, patient)** — only now is the 30-day history read, so two requests for the same pair can never both see the old total.
4. `evaluate_dispense(...)` (pure) returns all violations or the governing rule.
5. Accepted: insert the `dispenses` row (with its `rule_id`) and an `accepted` attempt. Rejected: insert only a `rejected` attempt with its reasons. **Both commit**; the HTTP 201/422 is chosen after the commit. Any error rolls everything back, so retrying with the same key is always safe.

## Data and invariants enforced by PostgreSQL itself

`formulary_rules` holds half-open date ranges `[effective_from, effective_to)` in business dates and an `EXCLUDE USING gist (medicine_id WITH =, daterange(...) WITH &&)` constraint: overlapping periods are impossible whatever the application does. `dispenses` references its governing rule through a composite foreign key `(rule_id, medicine_id)`. `dispense_attempts` has a CHECK that a rejection has reasons and no dispense, and an acceptance the opposite; its unique key is the idempotency record. Timestamps are `timestamptz` (UTC); every connection runs with `timezone=UTC`.

## Tests

`backend/tests/`: `unit` (engine, time, cursor, no database; includes hypothesis properties against brute-force oracles), `db` (migrations up/down, the data migration, constraints, raw concurrent-insert race), `api` (every endpoint and error path, a differential test against an independent model), `concurrency` (the deterministic overlap demonstration, negative controls, stress, supersede races), `seed`, `perf` (query plans, latency). `frontend/`: 288 headless unit tests.
