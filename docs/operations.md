# Operations runbook

Everything here runs from the repository root against the Compose stack. Commands that need the
operator key read it from the container environment, so the key never appears on a command line
or in shell history. Never print `.env`.

Shortcut used below (psql inside the database container):

```sh
alias relaydb='docker compose exec -T db psql -U relay -d relay -tA'
```

## Where to look

| What | Where |
| --- | --- |
| Service state | `docker compose ps` (api, worker, receiver, db are `healthy`; `migrate` exits 0) |
| Logs | `docker compose logs --tail=100 api worker receiver db`. API, worker and receiver write one JSON object per line (`ts`, `level`, `logger`, `message` plus fields such as `request_id`, `event_id`, `attempt`, `outcome`). Secrets, tokens, signatures and payloads are never logged. |
| API readiness | `curl -s localhost:8000/health/ready` (database reachable and migration revision current; 503 otherwise) |
| Process liveness | `curl -s localhost:8000/health/live` |
| Worker liveness | `curl -s localhost:8000/health/worker` (`online` when a heartbeat is under 30 s old) |
| API metrics | inside the api container on port 9090: `docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9090/metrics').read().decode())"` |
| Worker metrics | same, port 9091, in the worker container. Neither port is published and the public port returns 404 for `/metrics`. |

Useful metrics: `events_accepted_total`, `delivery_attempts_total{outcome}`,
`deliveries_terminal_total{status}`, `delivery_attempt_duration_seconds`, `worker_inflight`,
`quota_rejections_total{reason}`, `http_requests_total{route,status}`.

Delivery state lives in two tables. `deliveries` has one row per event (`status` is `pending`,
`processing`, `retry_wait`, `delivered` or `failed`, with `attempts_used`, `next_attempt_at`,
`lease_expires_at`, `last_error_code`). `delivery_attempts` has one row per attempt (`outcome`
is `in_progress`, `delivered`, `failed` or `unknown`).

A quick overview of the queue:

```sh
relaydb -c "SELECT status, count(*) FROM deliveries GROUP BY 1 ORDER BY 1"
```

## Symptom, check, fix

### Worker stopped or crashed

- **Symptom:** new events stay `pending`; `/health/worker` reports `offline` (up to 30 s after the stop).
- **Check:** `docker compose ps worker`, `docker compose logs --tail=50 worker`, and
  `relaydb -c "SELECT status, count(*) FROM deliveries GROUP BY 1"`.
- **Fix:** `docker compose start worker` (or `docker compose up -d worker`). Pending work is
  claimed immediately; nothing is lost because the queue is the database. A claim interrupted
  mid-attempt is recovered under *Expired lease*.
- **Exercised:** worker stopped, event accepted (202) stayed `pending` with 0 attempts, worker
  started, event reached `delivered` with 1 attempt within seconds.

### Database outage

- **Symptom:** `/health/ready` and every database-backed endpoint answer 503 with a JSON error
  (`{"error":{"code":"service_unavailable","message":"Database unavailable","request_id":...}}`);
  worker logs repeat `database_unavailable` and the worker backs off up to 30 s. No stack trace is returned.
- **Check:** `docker compose ps db`, `docker compose logs --tail=50 db`, `docker compose exec -T db pg_isready -U relay -d relay`.
- **Fix:** `docker compose start db`. API readiness returns 200 and the worker resumes on its next
  retry without a restart. If the data volume is damaged, restore the newest dump (see *Backup and restore*).
- **Exercised:** `docker compose stop db` gave 503 JSON without a stack trace; after
  `docker compose start db` readiness returned 200 within 15 s and the worker was `online`
  again; api, receiver and worker stayed `healthy` throughout.

### Failed destination

- **Symptom:** deliveries sit in `retry_wait` with `last_error_code` such as `connection_error`,
  `timeout` or an HTTP-status code, then become `failed` after five attempts. Retries use
  5/15/45/120 s plus up to 20% jitter (production profile).
- **Check:**
  ```sh
  relaydb -c "SELECT event_id, status, attempts_used, last_error_code, next_attempt_at FROM deliveries WHERE status IN ('retry_wait','failed') ORDER BY updated_at DESC LIMIT 20"
  relaydb -c "SELECT attempt_number, outcome, http_status, duration_ms FROM delivery_attempts WHERE event_id = '<id>' ORDER BY 1"
  ```
- **Fix:** correct `DESTINATION_URL` in `.env` (https, except the bundled demo receiver) and run
  `docker compose up -d worker`. Events still in `retry_wait` are retried on schedule with the new
  destination. An event that already reached `failed` used its five attempts and cannot be re-queued
  (attempt numbers are unique per event); resubmit it from the source with a new `Idempotency-Key`.
- **Exercised:** with `DESTINATION_URL` pointing at a closed port, an event moved to `retry_wait`
  (`connection_error`, attempt 1); after restoring the value and recreating the worker, the event
  was `delivered` on attempt 3 (attempts 1 and 2 stayed `failed` in history).

### Expired lease

- **Symptom:** a delivery is `processing` and `lease_expires_at` is in the past; the worker was
  killed or lost the database mid-attempt.
- **Check:** `relaydb -c "SELECT event_id, attempts_used, lease_expires_at FROM deliveries WHERE status='processing' AND lease_expires_at < now()"`.
- **Fix:** none needed while the worker runs. Each loop it recovers up to 100 expired leases:
  the interrupted attempt is marked `unknown`, `last_error_code` becomes `lease_expired`, and the
  delivery is rescheduled (or `failed` if the five-attempt budget is spent). If no worker runs,
  start it. The default lease is 60 s (`LEASE_DURATION`, minimum 60).
- **Exercised:** see *Stuck in processing*, which uses the same recovery path.

### Stuck in `processing`

- **Symptom:** `deliveries` rows stay `processing` for longer than `LEASE_DURATION`.
- **Check:** `relaydb -c "SELECT event_id, attempts_used, lease_expires_at - now() AS remaining FROM deliveries WHERE status='processing'"`.
  A positive `remaining` means an attempt is legitimately in flight (the request deadline is at
  most 10 s) or the worker died less than one lease ago; wait for it to expire. Also check
  `/health/worker`.
- **Fix:** ensure the worker is running; recovery is automatic once the lease expires. Do not edit
  the rows by hand: finalization is fenced by the lease token.
- **Exercised:** with the worker stopped, a delivery was put in `processing` with a lease expiring
  in 20 s and an `in_progress` attempt. After the worker started it stayed `processing` while the
  lease was live; after expiry the attempt became `unknown`, the delivery moved to `retry_wait`
  (`lease_expired`) and was then `delivered` on attempt 2.

### Quota exhausted (demo endpoints)

- **Symptom:** `POST /demo/runs` answers 429 `quota_exceeded` with `Retry-After`;
  `quota_rejections_total{reason=...}` increases.
- **Check:** the metric above, plus the limits in [sandbox.md](sandbox.md): 5 creations per client
  per rolling minute, 30 globally per minute, 100 active deliveries, 1,000 per UTC day, and the
  database size limit `DEMO_STORAGE_LIMIT_BYTES`.
- **Fix:** wait for `Retry-After`. For storage, find out what grew
  (`relaydb -c "SELECT pg_size_pretty(pg_database_size('relay'))"`) before raising
  `DEMO_STORAGE_LIMIT_BYTES`; the worker removes terminal demo runs 24 hours after their token expired (see
  sandbox.md). Never delete the data volume to clear a quota. Operator events are not subject to
  demo quotas.
- **Exercised:** six demo creations in one minute from one client: five 202, the sixth 429 with
  `Retry-After: 60`; the API metric `quota_rejections_total{reason="client_rate"}` read 1.

### Backup and restore

```sh
make backup                                   # backups/relay-<UTC timestamp>.dump, newest 7 kept
make restore FILE=backups/relay-<timestamp>.dump          # asks for confirmation
make restore FILE=backups/relay-<timestamp>.dump CONFIRM=yes   # non-interactive
```

- `make backup` streams `pg_dump -Fc` from the running db container into a `.partial` file, checks
  it with `pg_restore --list`, then renames it; a failed dump is never kept. It refuses to run if
  the db service is down. Dumps are written with mode 600 in `backups/` (git- and docker-ignored).
  Copy them off the machine for real protection.
- `make restore` checks the file is a readable custom-format dump, asks for confirmation (a
  non-interactive shell without `CONFIRM=yes` aborts without touching data), stops api, worker and
  receiver, runs `pg_restore --clean --if-exists --no-owner`, then starts everything again and
  waits for healthy. It replaces all current data, including the migration revision.
- After restoring a dump from an older schema, the migration job runs on startup and upgrades it.
- **Exercised:** 30 events / 64 attempts / 23 receiver effects were backed up, `make clean && make up`
  produced an empty database (0/0/0), `make restore ... CONFIRM=yes` brought back exactly
  30/64/23 and all services were healthy. A missing file and a non-dump file were rejected with a clear
  message; a non-interactive run without `CONFIRM=yes` aborted with data unchanged; answering `no`
  at the prompt aborted. Nine backups in a row left exactly seven files and no `.partial` file.

### Rollback

```sh
make rollback VERSION=<git tag or commit>        # asks for confirmation; CONFIRM=yes to skip
make up                                          # roll forward again from the current checkout
```

`make rollback` takes a backup first, exports the ref with `git archive` into a temporary directory
(the working tree is untouched), and builds and starts that version with the same Compose project, so
it shares the data volume. Its image is tagged `webhook-relay:rollback-<short sha>`; the current tag is
kept. The temporary copy (including its private copy of `.env`) is deleted on exit.

Schema handling: the script finds the target's newest migration. If the database revision is not
known to the target (newer), it runs `alembic downgrade <target head>` using the **current** image
(which has the downgrade scripts) before switching. If the target is as new as the database, no
migration is run. Downgrades can drop columns and tables; the backup taken first is the way back
(`make restore`). Rolling forward with `make up` runs `alembic upgrade head`.

- **Exercised:** `make rollback VERSION=a0c4e0f` on the running stack (same migrations as current):
  backup taken, no schema change, all services healthy on the rollback image, event count unchanged;
  `make up` returned to the current image, healthy, data intact. The downgrade branch was exercised
  once on isolated data (separate Compose project and volume, never the real data) using a synthetic
  commit that lacks the newest migration: the script downgraded `0004` to `0003`, the `demo_runs.client_key`
  column was gone, the old tree was started and answered `/health/ready` with 200, and `make up`
  upgraded back to `0004` with both events present. `alembic downgrade 0003` followed by
  `alembic upgrade head` also runs in the test database.

### Rotating `OPERATOR_API_KEY` and `SIGNING_SECRET`

All services read `.env`, so edit it and recreate the stack; Compose recreates containers whose
environment changed.

1. Generate values without printing them, for example
   `python3 -c "import secrets; print(secrets.token_hex(32))"` (at least 32 characters each), and edit
   the `OPERATOR_API_KEY` / `SIGNING_SECRET` lines of `.env` in an editor.
2. `docker compose up -d --wait` (worker, receiver and api must all restart together after a
   `SIGNING_SECRET` change: the worker signs and the receiver verifies).
3. Verify: the old key gets 401, the new key gets 202, and a new event reaches `delivered`.
4. Give the new `OPERATOR_API_KEY` to producers. Do not rotate `POSTGRES_PASSWORD` this way: it is
   stored in the data volume, and `DATABASE_URL` must keep matching it.

Events already stored keep their outgoing bytes; they are signed at delivery time with the secret
current then, so retries after a rotation use the new secret.

- **Exercised:** both values replaced, stack recreated: old key 401, new key 202, the new event
  `delivered` (signature accepted by the receiver); the original values were then restored the same way.

## Resource footprint

Measured with `docker stats --no-stream` after the stack had been idle for about two minutes
(four running containers, empty queue, Docker 29.1.3, Compose 2.40):

| Container | Memory used | Limit | CPU (idle) |
| --- | --- | --- | --- |
| api | 64.0 MiB | 192 MiB | 0.2% |
| worker | 59.9 MiB | 192 MiB | 1.0% |
| receiver | 58.0 MiB | 192 MiB | 0.3% |
| db | 34.0 MiB | 256 MiB | 6.6% |
| **Total** | **215.9 MiB** | 832 MiB | |

The target is under 1 GiB for the whole stack: measured idle usage is about 216 MiB (21% of the
target), and the combined container limits (832 MiB) are themselves below it. The one-shot `migrate`
container exits after migrating and holds no memory. Memory grows with load and database size; these
are idle numbers, not a throughput claim.
