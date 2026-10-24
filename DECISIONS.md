# Decisions

Each entry: what was chosen, what was considered instead, what it costs. Ambiguities in the brief are
resolved in ADR 6 to 10 and collected in ADR 10.

## 1. Rule versioning: one row per period, non-overlap enforced by the database

**Chosen.** `formulary_rules` has one row per medicine and period, `[effective_from, effective_to)` as
`date` columns (`effective_to` exclusive, `NULL` = open-ended), plus
`EXCLUDE USING gist (medicine_id WITH =, daterange(effective_from, effective_to, '[)') WITH &&)`.
Overlap is therefore impossible however the rows arrive, which the brief demands ("enforce it"). Every
dispense stores the `rule_id` that governed it (composite FK, so the rule must belong to the same medicine).
`POST /medicines/{code}/rules` with date D, in one transaction under `FOR UPDATE` on the medicine row:
if a rule already starts on D, 409 (nothing would remain of it); if a rule is in force on D, its
`effective_to` is cut to D and the new rule inherits its old end; otherwise the new rule runs until the next
rule starts (or forever). No row is ever deleted or moved; the only mutation is the truncated end date. A
change is also refused (409) when it would re-govern an already recorded dispense, so "a dispense in March is
governed by March's rules" cannot be broken retroactively.
**Considered.** (a) One mutable row per medicine plus an audit table: the history is then a log, not data the
rule lookup can use. (b) SCD2 with an `is_current` flag or triggers maintaining `valid_to`: invariants live in
convention. (c) `SELECT` then `INSERT` overlap check in application code: two simultaneous writers can both pass it. (d) `tstzrange` of instants: pushes the SAST conversion into every writer; the
domain's unit is the business day. (e) Inclusive `effective_to`: off-by-one gaps; half-open ranges tile.
(f) Replacing or deleting future rules on supersede: destroys history.
**Gave up.** Sub-day effectiveness; append-only rows (the end date of the truncated rule changes); a
`btree_gist` dependency (a trusted extension on PostgreSQL 13+); retroactive corrections need a deliberate
process because they are refused. Concurrent raw inserts of overlapping periods can fail with deadlock
(SQLSTATE 40P01, measured in 2 of 15 two-way races) instead of 23P01; exactly one insert won in all 60 races.
The service path avoids it (the medicine lock serialises supersedes) and maps both to a clean 409/503.

## 2. Concurrency: transaction-scoped locks in a fixed order, READ COMMITTED

**Chosen.** Order: (1) advisory lock on the idempotency key, (2) `FOR SHARE` on the medicine row, (3) advisory
lock on (medicine, patient); the 30-day history is read only after (3), so two requests for one pair cannot both
see the old total. Rule changes take `FOR UPDATE` on the medicine row, so a dispense validated against old rules
cannot slip in beside a retroactive supersede. Locks are `pg_advisory_xact_lock` (released at commit/rollback),
keys are 64-bit blake2b hashes with a namespace; `lock_timeout` (5 s) turns waiting into a retryable 503.
**Demonstration** (`tests/concurrency/test_overlap.py`): request A is parked after reading its history while
holding its locks; B is started only then; a monitor connection waits until `pg_stat_activity` and `pg_locks`
show B blocked on exactly A's pair lock; A is released; B reads A's quantity and is rejected. A negative control
removes the lock and asserts the breach (120 > 100). Also 16-way barrier stress, same-key duplicates, supersede
races. 12 mutations (no pair lock, history read before the lock, no medicine lock, no key lock, REPEATABLE READ
snapshot, window from `now()`, insert-then-delete, ...) each make at least one test fail; the concurrency suite
was repeated 25 times on each of three successive trees (75 runs): 0 flakes.
**Considered.** `SERIALIZABLE` plus retries (correct, but every conflicting request needs a retry loop and
false positives rise with load); `FOR UPDATE` on the medicine (simple, serialises every patient of a medicine);
a counter row per pair (backdating makes a single counter meaningless); a trigger summing in the database
(unbypassable, but hides business logic in SQL); a table lock.
**Gave up.** Requests for one patient and medicine queue behind each other (the critical section is a few
indexed statements, milliseconds). The guarantee holds for every writer that uses the
service; a writer bypassing it bypasses the lock (the unique keys and constraints still hold). `FOR SHARE` writes
a tuple lock per dispense; at 10x I would move that fence to a shared advisory lock. Transaction-scoped advisory
locks stay correct behind a transaction-pooling PgBouncer; session-level ones would not.

## 3. Pagination: keyset (cursor) everywhere

**Chosen.** `limit` (default 25, max 100) and an opaque `cursor`; response `{items, next_cursor}`. Medicines
order by `(name, code)` (index `ix_medicines_name_code`), dispenses by `(dispensed_at DESC, id DESC)`; the
predicate is a row comparison so the index seeks to the cursor. The cursor is base64url JSON with a version, the
last sort key and a hash of the filters: reusing it under different filters, or tampering, gives 400
`invalid_cursor`. The SPA keeps the cursor trail in the URL, so reload and back/forward work.
**Considered.** `LIMIT/OFFSET` with a page number: cost grows with depth and pages shift while rows are
inserted (backdated dispenses make that routine); a total count: `COUNT(*)` is exactly the cost keyset avoids.
**Gave up.** Jump-to-page and totals ("25 medicines on this page" instead of "of 504"); a backdated dispense
inserted into a range already served is not seen until the walk restarts.

## 4. Error contract: RFC 9457 problem+json with a uniform `violations` list

**Chosen.** Every non-2xx response, from any handler, has `type`, `title`, `status`, `detail`, `code`,
`request_id` (= `X-Request-ID`) and a `violations` array of `{code, field, message, context}`. `field` is the
request field name (or null), so a client maps a rejection to a form control; `code` is the stable rule or cause;
`context` carries numbers (limit, requested, already dispensed). A dispense rejection lists every violated rule
(and an `attempt_id`). Statuses: 422 `validation_error` / `dispense_rejected` / `idempotency_key_reuse`, 400
`invalid_cursor`, 404, 409 `rule_conflict`, 503 `resource_busy` (lock timeout or deadlock, `Retry-After`) /
`service_unavailable`, 500 `internal_error` (generic, never internals). A conformance test walks every error path.
**Considered.** Bare `{"error": "..."}` strings (no field mapping); per-endpoint shapes; JSON:API errors.
**Gave up.** A little verbosity; validation violation codes are our own small set mapped from Pydantic.

## 5. Indexing, and how it was validated

**Chosen.** `ix_medicines_name_code` (keyset order); GIN trigram indexes on `name` and `code` (substring
search); `ix_formulary_rules_medicine_id_effective_from` (history read; the GiST index of the exclusion
constraint serves enforcement); on `dispenses`: `(patient_ref, medicine_id, dispensed_at DESC, id DESC)` (the
locked quota read and the patient ledger), `(medicine_id, dispensed_at DESC, id DESC)` (medicine filter,
history-immutability check), `(dispensed_at DESC, id DESC)` (unfiltered list). No index on `dispense_attempts`
beyond its unique keys: nothing reads it.
**Validation.** (1) `tests/perf/test_query_plans.py` `EXPLAIN`s exactly what the repositories send, on 153,000
medicines and 30,000 dispenses, and asserts the intended index and no sequential scan; mutations (ILIKE to
`lower() LIKE`, instant-only cursor predicate) fail it. (2) It found a trap: psycopg prepares a statement after
five uses and PostgreSQL then adopts a generic plan, which made the trigram search two orders of magnitude
slower; the search sets `plan_cache_mode = force_custom_plan` and a test executes it 20 times. (3) Honest
limits, measured: at the brief's 500 medicines PostgreSQL correctly prefers a sequential scan (0.23 ms vs 0.29 ms
forced index); it starts choosing the trigram indexes at about 100,000 rows (8 to 12x faster there); terms
shorter than 3 characters can never use them (a no-match 2-letter term costs 57 ms at 150,000 rows).
(4) **Latency**, `docker compose exec api python -m app.bench` (HTTP, keep-alive, 300 requests after warm-up,
nearest-rank percentiles, first page of every list/read endpoint, parameters drawn from the live data), inside
the compose stack on a 4-core Windows laptop with PostgreSQL in Docker: 1x data (504 medicines, 6,010 dispenses)
slowest p95 5.8 ms; 10x data (5,004 / 60,010) slowest p95 8.3 ms at 1 client and 75.6 ms at 8 concurrent clients;
the budget is 300 ms. One uvicorn process serves about 300 to 350 requests/s here, so p95 would cross 300 ms at
roughly 50 to 60 simultaneous clients per process; most of a 4 ms request is round trips to the database.
**Gave up.** Write amplification of three secondary indexes on `dispenses` and three on `medicines`;
patient-only ledgers sort a patient's rows (a patient with 20,000 dispenses still lists in 3 ms, so no extra index).

## 6. Time: UTC at rest, business days in Africa/Johannesburg

Timestamps are `timestamptz`; every connection sets `timezone=UTC`. A business day is a SAST calendar date.
`app/domain/timeutil.py` holds the only two conversions (instant to business date, business date to its first
UTC instant); SQL `WHERE` clauses compare plain instants. Rule periods and the 30-day window are in business
dates, so `2026-02-28T22:30:00Z` (00:30 SAST on 1 March) belongs to the rule starting 1 March; the seed
cross-checks Python's time zone against PostgreSQL's `AT TIME ZONE` for every row, and migration 0003 does its
backfill with `AT TIME ZONE` in SQL. `dispensed_at` must carry an offset (naive is ambiguous, 422). The SPA
never uses the browser zone (converts via `Intl`; tested under other zones). South Africa has no DST, but
nothing assumes a fixed +02:00.

## 7. Rejections and the attempt log

Every request that is well formed enough to evaluate leaves exactly one row in `dispense_attempts`
(accepted or rejected with reasons), in the same transaction as the dispense it may create. The rule engine
runs before any insert, so a rejection never writes a `dispenses` row and the attempt row can still commit
(insert-then-rollback would roll back the log too). A CHECK constraint states the shape. Malformed requests
(schema failures) are not attempts and are only logged by the application. Failure injection tests prove a 500
leaves nothing behind. Cost: one extra row per request; the table needs retention at scale.

## 8. Idempotency: same key, different payload is 422

The key is global (no authenticated principal; with auth it would be per principal). The attempt row stores a
hash of the normalised payload: equal hash replays the original status and body (header `Idempotency-Replayed`),
different hash is **422 `idempotency_key_reuse`**: it is a client bug, retrying cannot succeed, and it follows the
IETF Idempotency-Key draft; 409 would suggest a state conflict that a retry might resolve. Omitting
`dispensed_at` versus sending it is a different payload. Rejections are replayed too, so a client that wants
re-evaluation must use a new key (the SPA does that after any 2xx/4xx and reuses the key only after failures with
no definitive answer).

## 9. Dispense window and backdating

The 30-day window is 30 business dates `[d-29, d]`; a dispense 30 days earlier is outside. It counts dispenses
by `dispensed_at`, never by when they were recorded. The invariant is "every recorded dispense's window total
stays within the cap of the rule in force on its date", whatever the insertion order, so admitting a backdated
dispense also checks every later recorded dispense whose window now contains it (`later_dispense_limit_exceeded`).
Hypothesis tests insert random dispenses in random order against brute-force oracles. Cost: a backdated record
can be refused because it would push a later dispense over its cap.

## 10. Assumptions and what was left out

Future-dated dispenses are allowed (validated against the rule on that date). Medicine codes are exact,
case-sensitive; search is case-insensitive substring. `quantity` 1 to 1,000,000; strings trimmed, no control
characters. No endpoint creates or deactivates medicines (the brief lists none). The attempt log has no read
endpoint (query the table). No authentication (out of scope). Frontend: Angular 21, because the installed Node
24.11 is below Angular 22's minimum; tested in Chrome only. Seed counts are generated rows plus 4 demo
medicines and `PT-DEMO` (504 / 6,010 / 2,501), and history ends yesterday so no seeded row is in the future.
