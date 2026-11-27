# ADR 0002: Lease fencing and recovery

Status: accepted

## Context

A worker can crash, be killed, lose the database or stall while it holds a delivery. The delivery must become
eligible again, but a worker that was only slow must not later overwrite the state written by whoever took over.
HTTP calls take seconds, so a database lock cannot be held for their duration.

## Decision

Ownership of a delivery is a lease stored on its row:

- **Claim** (`claim` in `app/worker/main.py`): in one short transaction the worker sets `status = 'processing'`,
  increments `attempts_used`, writes a new random `lease_token` and `lease_expires_at` (now plus
  `LEASE_DURATION`, default and minimum 60 seconds) and inserts a `delivery_attempts` row carrying the same
  token with outcome `in_progress`, then commits. The attempt is reserved before any HTTP happens.
- **Finalize** (`finalize`): the update runs `WHERE status = 'processing' AND lease_token = :token AND
  lease_expires_at > :now` under `FOR UPDATE`. A worker whose lease expired, or was replaced, matches no
  row and writes nothing. The attempt row and the delivery row change in the same transaction.
- **Recover** (`recover_expired`): before each claim, a worker selects up to 100 `processing` rows with an
  expired lease using `FOR UPDATE SKIP LOCKED`, marks their attempt `unknown`, clears the lease, and sets
  `retry_wait` with the normal delay, or `failed` if five attempts are already used. `last_error_code` becomes
  `lease_expired`.
- The attempt number stays counted even when the outcome is `unknown`, so crashes consume the budget and
  retries stay bounded.

Request deadlines (at most 10 seconds) are far below the lease, so a healthy attempt always finalizes inside it.

## Consequences

- A crashed worker delays its delivery by up to one lease (60 seconds by default), not forever.
- The expiry check in `finalize` means a stale worker is rejected even if recovery has not yet run.
- Recovery and finalization lock the same `deliveries` row, so two workers cannot recover one expired row twice.
- The cost of safety is a possible duplicate: a worker may have delivered the request and died before
  finalizing, so recovery will send it again ([ADR 0004](0004-at-least-once-with-receiver-dedup.md)).
- Lease times come from the worker's UTC clock, so clock skew between worker hosts shifts expiry by that
  amount; the 60-second minimum lease leaves a wide margin, and the demo runs workers on one host.

## Alternatives considered

- **Hold the row lock during HTTP.** Simple, but pins a transaction and a connection for seconds, and a
  crash inside it is invisible to others until the connection times out.
- **Advisory locks per delivery.** Release on session end, which couples correctness to connection lifetime
  and leaves no durable record of the interrupted attempt.
- **Heartbeat extension of the lease.** Not needed while the per-attempt deadline is bounded well below the
  lease; adding it would add moving parts without a case that requires it.
