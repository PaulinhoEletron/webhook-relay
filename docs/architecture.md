# Milestone 1 decisions

- Preserve the specified Python 3.12 / FastAPI / PostgreSQL 16 stack. No extra queue or frontend framework.
- PostgreSQL owns idempotency and scheduling in one transaction. An API crash before commit rolls everything back; after commit the pending event survives the API process.
- Deterministic JSON (sorted keys, fixed separators, UTF-8, finite numbers) produces the fingerprint. JSON numeric representations such as `1` and `1.0` remain distinct. An immutable envelope is stored as bytes.
- Worker claims use `SKIP LOCKED`; HTTP runs outside transactions. Lease tokens fence stale finalization. Full retry and lease-recovery behavior is deliberately deferred to Milestone 2.
- HTTPX phase timeouts are supplemented by an asyncio wall-clock deadline. Responses are streamed and discarded without buffering arbitrary downstream response bodies.
- Three core tables are migrated now. Demo runs, durable receiver effects and worker heartbeats will be added with the milestones that implement them, avoiding unused schema in the first slice.
- HMAC signing, verification and attempt inspection were small additions to make this slice inspectable. Receiver business-effect deduplication is still unimplemented.
- API and worker reuse one non-root, read-only image. A single migration job gates startup. The database volume survives `make down`.
- This preview stays on localhost. Sandbox quotas and cleanup are implemented; there is no public tunnel, live link, or paid service. See [sandbox limits](sandbox.md).

Implementation references: [PostgreSQL 16 row locking](https://www.postgresql.org/docs/16/sql-select.html), [SQLAlchemy PostgreSQL ON CONFLICT](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert), and [HTTPX phase timeouts](https://www.python-httpx.org/advanced/timeouts/).
- Idempotency keys use printable ASCII (1–128 characters), excluding control characters that are unsafe in HTTP headers or PostgreSQL text. This narrows the specification's general ASCII wording.

## Step 3: bounded retry scheduling

- The existing migration already includes `retry_wait`, due-time indexing and the five-attempt database constraints; no schema migration is needed.
- Claims include due `pending`/`retry_wait` rows with remaining budget. HTTP remains outside transactions; finalization keeps the lease-token check and stores the attempt and next delivery state atomically.
- Individual unsuccessful attempts retain outcome `failed`; delivery state becomes `retry_wait` only for a retryable result with budget remaining. No request sleeps while holding a database transaction.
- Retry policy lives in `app/worker/retry.py`. Controlled time and jitter inputs make boundary tests deterministic, while integration tests use real PostgreSQL and real HTTP responses.
- Existing environments default to the production profile. Accelerated timings require explicit `RETRY_PROFILE=demo` with demo mode enabled. Public per-run profiles remain part of the future sandbox work.
- At the end of step 3, expired leases and receiver effect deduplication remained pending; step 4 below implements them.

## Step 4: recovery and durable receiver effects

- Before claiming new work, each worker recovers at most 100 expired processing rows in a short transaction, ordered by expiration and locked with `SKIP LOCKED`. Recovery and finalization lock the same delivery row. Competing workers cannot recover it twice.
- Recovery marks the reserved attempt `unknown`, leaves its number counted, clears the lease and uses the configured retry delay. The fifth interrupted attempt becomes terminal failure. An expired lease cannot finalize even if recovery has not run yet; a replaced token cannot overwrite the next attempt.
- SIGTERM stops the loop from claiming another job and allows the already bounded HTTP request to finish. Abrupt exits leave a durable lease for recovery.
- Migration `0002` adds `fulfillment_effects`. A row keyed by stable event ID is the example receiver's simulated business effect. `INSERT ... ON CONFLICT DO NOTHING` commits before HTTP 200. No separate deduplication marker can commit without the effect. Database failure returns a sanitized 503 and the transaction rolls back.
- Receiver effects have no foreign key to relay events. This models downstream ownership and avoids losing deduplication when relay data is deleted; the local demo shares one PostgreSQL database for simplicity. No external side effect (email, payment, etc.) is claimed to be atomic with the insert.
- Compose now waits for migration success before starting the receiver. Existing relay events/attempts survive the migration; historical receipts are not backfilled.
- Tests kill real subprocesses after committed claims and after real receiver success, then advance a controlled clock through expiry. They verify redelivery with one effect, concurrent recovery/claims, stale finalization, shutdown and receiver restart.
