# ADR 0001: PostgreSQL as the queue

Status: accepted

## Context

The service must persist an event and schedule its delivery together, retry on a timetable, and survive
restarts of either process. The expected volume is small (one destination, a public sandbox limited to a
few runs per minute) and the host is a single small machine. Whatever holds the work must never disagree
with the stored event about whether the event exists.

## Decision

Delivery work is a row in the `deliveries` table, next to its `events` row, and PostgreSQL is the only
datastore. `POST /v1/events` inserts the event and its delivery in one transaction, so the event exists if
and only if its delivery is scheduled. The worker polls about once a second for due work:

- eligible rows have `status` in (`pending`, `retry_wait`), `next_attempt_at` not in the future and
  `attempts_used < 5`, found through the `ix_delivery_due` index on `(status, next_attempt_at)`;
- a row is claimed with `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` (`claim` in `app/worker/main.py`), so
  several workers never receive the same row;
- retries are scheduled by writing `next_attempt_at`, not by a separate timer.

HTTP calls happen outside the claiming transaction; ownership during the call is a lease (see
[ADR 0002](0002-lease-fencing-and-recovery.md)).

## Consequences

- No dual write: there is no state in which an event is accepted but its job is lost, or a job exists for an
  event that was never committed.
- One system to run, back up and restore; `make backup` captures queue and data together.
- Queue state is queryable with SQL, which the runbook uses for diagnosis.
- Latency is bounded by the poll interval (about one second) rather than push-based.
- Throughput is limited by what one PostgreSQL instance can do for row-lock polling. That is far above this
  project's needs but it is not a high-throughput broker, and no benchmark is claimed.
- A long-held transaction would stall other workers on the same row; the design avoids it by never holding
  a transaction across HTTP.

## Alternatives considered

- **Redis or Celery.** Adds a second datastore and a dual-write failure: the event commits but the enqueue
  fails (or the reverse). Closing that gap needs an outbox, which is the table this design already uses.
- **Kafka or a managed queue.** Operationally heavier than the problem, and unavailable at zero hosting cost.
- **LISTEN/NOTIFY.** Could cut latency, but polling is simpler, survives missed notifications by
  construction, and one second is acceptable here.
