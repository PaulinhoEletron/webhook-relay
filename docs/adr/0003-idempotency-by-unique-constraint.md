# ADR 0003: Idempotency by unique constraint

Status: accepted

## Context

Producers retry. A retried `POST /v1/events` after a timeout must not create a second event or a second
delivery, even when two copies of the request arrive at the same moment on different API processes. A key
reused for a different request is a client bug that must be visible.

## Decision

The database decides. `events` has the constraint `uq_event_key` on `(scope_id, idempotency_key)`, and `ingest`
(`app/services/ingestion.py`) works in one transaction:

1. Validate the request and serialize it deterministically (sorted keys, fixed separators, UTF-8, finite
   numbers only); the SHA-256 of that is the `fingerprint`.
2. `INSERT INTO events ... ON CONFLICT ON CONSTRAINT uq_event_key DO NOTHING RETURNING id`.
3. If a row was inserted, insert its `deliveries` row in the same transaction. If not, read the existing
   event by key. At READ COMMITTED, a concurrent identical insert makes the second statement wait for the
   first transaction, so the read sees the committed winner.
4. Compare fingerprints: equal means a replay (`200`, `Idempotency-Replayed: true`, nothing enqueued); different
   means `IdempotencyConflict` and a `409`, leaving the original event unchanged.
5. The API answers only after the transaction commits.

The `scope_id` column separates namespaces: operator events use `operator`, demo runs use `demo`, so a visitor
cannot collide with, or probe, operator keys.

## Consequences

- Concurrent duplicates resolve to one event and one delivery without any in-memory lock or pre-check, across
  any number of API processes.
- The guarantee lasts as long as the row does: operator events and keys are retained until explicitly removed.
  Demo events are cleaned 24 hours after their token expires, together with their keys.
- Numerically different JSON values (`1` and `1.0`) have different fingerprints, so they conflict.
- The key shape is limited to printable ASCII, 1 to 128 characters.

## Alternatives considered

- **Check-then-insert in application code.** Racy under concurrency without extra locking.
- **Locking on the key (advisory lock) before inserting.** Works, but adds a round trip and a second mechanism
  where the constraint already provides the guarantee.
- **Redis `SET NX`.** A second datastore with its own failure modes and a gap between it and the committed row.
