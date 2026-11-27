# ADR 0004: At-least-once delivery with receiver deduplication

Status: accepted

## Context

Between the worker sending a request and recording the response, things can fail: the receiver may have
processed the request while the response is lost, the connection may reset, or the worker may be killed. The
worker cannot distinguish "never arrived" from "arrived, answer lost". Over plain HTTP no sender can provide
exactly-once delivery.

## Decision

The relay promises bounded at-least-once delivery and says so:

- Each event gets at most five reserved attempts (`ck_attempt_budget`, `ck_attempt_number`). Transient failures
  (connection errors, timeouts, HTTP 408, 429, 5xx) and interrupted attempts are retried; other results fail
  immediately. After the budget the delivery is `failed`; there is no promise of eventual delivery.
- Every attempt carries the same body and the stable event UUID in `X-Webhook-Id`, with a fresh timestamp and
  signature, so a receiver can recognize a repeat.
- The example receiver (`receiver/main.py`) makes its business effect idempotent by event ID:
  `INSERT INTO fulfillment_effects ... ON CONFLICT DO NOTHING` on the primary key `event_id`, committed before
  it answers `200`. The effect and its deduplication are one atomic insert, so there is no separate marker that
  could commit without the effect. Duplicates answer `200` without changing the first effect.
- The effect table has no foreign key to relay data, modelling a downstream system that owns its own state and
  keeps deduplicating after relay events are deleted.

## Consequences

- Receivers must deduplicate by event ID; this is documented in the README as part of the contract.
- The demonstration covers a database-backed effect only. It does not show that an arbitrary external action
  (email, payment) can be made exactly-once; that needs the receiver's own idempotency support.
- Retries consume the budget even when the first attempt had in fact succeeded, which is why the history keeps
  `unknown` and `failed` attempts visible.
- Tests kill real processes after a committed claim and after a real receiver success, advance a controlled
  clock through lease expiry, and assert redelivery with exactly one effect.

## Alternatives considered

- **Claim exactly-once delivery.** Not achievable without the receiver's cooperation, and claiming it would be
  dishonest.
- **At-most-once (never retry).** Loses events on transient errors, which defeats the purpose of the relay.
- **Unbounded retries or a dead-letter service.** More machinery than the scope allows; a `failed` row with
  retained history is sufficient, and resubmitting with a new idempotency key is documented.
