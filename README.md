# Webhook Relay

**Environment verified (2026-10-19):** native Docker is active; Compose startup, integration tests, delivery and restart persistence passed. See [implementation status](docs/implementation-status.md).

Accept an order event, persist it in PostgreSQL, and deliver it to a configured HTTP receiver through a separate worker.

**Implementation rules:** [10 steps with a mandatory full regression gate after each step](IMPLEMENTATION-POLICIES.md).

**Delivery progress:** [10-step roadmap](docs/delivery-roadmap.md) — 6 steps verified complete; next: the browser demonstration page.

**Local preview: bounded retries, crash recovery, durable receiver deduplication and isolated demo scenarios.** Public demo, screenshot, and public HTTPS link are not available yet. Interactive API documentation is at [localhost:8000/docs](http://localhost:8000/docs) after startup.

## Try it

Requires Docker Engine with Compose and Python 3 for local secret generation:

The Docker host conflict was repaired by switching to the installed native daemon. Application security settings remain enabled. Historical repair and rollback instructions are in [Docker host repair](docs/docker-host-repair.md).

```sh
make init   # creates private .env; leaves an existing file unchanged
make up     # builds, migrates, starts services, waits for health checks
make demo   # posts a synthetic order and prints its actual delivery history
```

The API binds only to `127.0.0.1:8000`. PostgreSQL and the receiver have no published ports. Set the `X-API-Key` authorization in `/docs` using the local `OPERATOR_API_KEY` value in `.env`; do not share that file.

## Architecture and contract

```text
Producer -> FastAPI -> PostgreSQL <- Worker -> HTTP receiver
```

- `POST /v1/events`: `X-API-Key` and `Idempotency-Key` required; returns 202 after commit.
- Same key and validated content: 200, same event ID, `Idempotency-Replayed: true`.
- Same key with different content: 409; original event remains unchanged.
- `GET /v1/events/{id}` and `/v1/events/{id}/attempts`: authenticated status/history.
- `/health/live`: process check. `/health/ready`: database and migration revision check.
- Bodies are limited to 16 KiB, including streamed requests. Unknown top-level fields and non-finite numbers are rejected.
- The receiver verifies HMAC-SHA256 signatures and a five-minute timestamp window.

The unique `(scope_id, idempotency_key)` constraint arbitrates concurrent requests. Event and delivery are created in the same transaction. `ON CONFLICT DO NOTHING` waits for an in-flight competitor; the next query reads the committed winning event. Acceptance occurs only after commit.

The worker claims a row with `FOR UPDATE SKIP LOCKED`, reserves an attempt and commits before making HTTP calls. Finalization checks the lease token. The outgoing bytes are stored at ingestion and stay unchanged. See [design notes](docs/architecture.md).

## Local development

Dependencies are pinned in `uv.lock`; containers use pinned versions/digests.

```sh
uv sync --frozen
make lint
make test       # isolated PostgreSQL relay_test database; no SQLite
make migrate    # explicit migration job
make down      # stops containers, preserves the application database volume
```

`make test` leaves the test database container available for subsequent runs. Stop it with `docker compose --profile test stop test-db`. Tests truncate **only** a database named `relay_test`. The runtime database is `relay`.

## Current failure semantics

Delivery reserves at most **five attempts**, including the initial attempt. Connection errors, timeouts, HTTP 408, 429 and 5xx schedule retries; other non-2xx responses (including redirects) fail immediately. Production delays are 5/15/45/120 seconds plus up to 20% positive jitter. Valid `Retry-After` seconds or dates on 429/503 increase the delay, capped at 15 minutes. Requests retain a 10-second overall deadline and a three-second connect timeout; redirects are not followed.

The worker recovers expired leases (60 seconds by default), marks interrupted attempts `unknown`, and schedules another attempt if the five-attempt budget remains. Expired or replaced lease holders cannot finalize a result. SIGTERM stops new claims and lets the bounded in-flight request finish. A lost response can still cause duplicate HTTP receipts: the example receiver records its simulated fulfillment effect once per stable event ID in PostgreSQL. Exactly-once HTTP delivery and eventual success after budget exhaustion are not promised.

`RETRY_PROFILE=production` is the default, including for existing `.env` files. `RETRY_PROFILE=demo` requires `DEMO_MODE=true` and uses accelerated 2/4/8/12-second delays without jitter; demo runs always use this accelerated schedule while operator events use the configured profile. A failed attempt remains `failed` in history while its delivery can be `retry_wait` and later succeed.

The example receiver stores its simulated fulfillment in `fulfillment_effects`, independently of operator event retention; expired terminal demo effects are cleaned with their runs. The unique event ID and the effect are the same atomic insert; duplicates return 200 without changing the first effect. This demonstrates database-backed effects, not deduplication of arbitrary external actions. Existing pre-migration deliveries are not backfilled with invented receiver effects.

Operator events and idempotency keys are retained indefinitely until explicitly removed. Expired terminal demo runs are cleaned by the worker after a further 24 hours. The browser page remains pending. `DEMO_MODE=true` enables the local demo endpoints and permits the exact internal Compose receiver URL. Demo creation and polling quotas, storage admission and cleanup are described in the [sandbox guide](docs/sandbox.md). Public deployment remains pending.

## Verification and operations

See [implementation status](docs/implementation-status.md) for actual checks and remaining milestones, and the [full regression checklist](docs/regression-checklist.md) for the mandatory gate after every step. CI and public deployment are not complete. No measured throughput or production availability is claimed.

Use `docker compose logs --tail=50 api worker receiver` to inspect execution. Use `docker compose restart worker` to resume pending jobs after stopping the worker; interrupted claims recover automatically after their leases expire. Database outages return sanitized 503 responses, and the worker backs off up to 30 seconds. Keep `.env` unchanged across restarts so its database password still matches the volume. Never use `docker compose down -v` unless deliberately deleting local data.

## Isolated local demo runs

`POST /demo/runs` accepts only `{"scenario":"success"}`, `temporary_failure`, or
`permanent_failure`. It returns a run ID, event ID, one-hour expiration and a random
`read_token`. Send that token in `Authorization: Bearer <token>` when reading the
returned `/demo/runs/{run_id}` status URL. Tokens are returned once, stored only as
SHA-256 hashes, and must not be placed in URLs. Responses contain actual ordered
attempts, without payloads or receiver response bodies. Demo responses use `Cache-Control: no-store`.

The scenarios produce HTTP 200, HTTP 503/503/200, and HTTP 400 respectively. Receiver
receipt counters are transactionally persisted per event; concurrent visitors do
not share counters. Demo retries use 2/4/8/12 seconds. The receiver's successful
fulfillment insert remains deduplicated by event ID.

`GET /health/worker` reports `online` when the worker has written a heartbeat within
30 seconds, otherwise `offline`. API/database readiness remains a separate check.
Set `DEMO_MODE=false` to disable both demo endpoints.

Exercise all scenarios locally without printing read tokens:

```sh
docker compose exec -T api python -m examples.demo_smoke
```

See [sandbox limits and proxy configuration](docs/sandbox.md) before configuring a reverse proxy.
