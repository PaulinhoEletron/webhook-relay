# Sandbox limits and retention

The public API accepts only a scenario enum. Payloads, destination URLs and headers
come from server configuration and synthetic fixtures. Unknown fields are rejected;
all API bodies have the existing 16 KiB streamed limit. Status responses expose only
run metadata and sanitized attempt fields. Tokens are sent in the Authorization
header, stored as hashes, and omitted from application logs; demo responses are not cached.

## Admission

Creation checks and inserts share a PostgreSQL transaction and a transaction-level
advisory lock. Limits hold across API processes and survive restarts:

| Limit | Window |
| --- | --- |
| 5 accepted creations per client | Rolling 60 seconds |
| 30 accepted creations globally | Rolling 60 seconds |
| 100 active demo deliveries | Pending, processing, or retry_wait |
| 1,000 accepted creations globally | UTC calendar day |
| Database storage | `DEMO_STORAGE_LIMIT_BYTES`, default 268435456 (256 MiB) |

Rejected work inserts no run/event and consumes no creation allowance. Successful
creation records themselves supply the counters; retention keeps them longer than
the daily quota window. Operator events do not count against demo event quotas.
The storage check uses PostgreSQL `pg_database_size(current_database())`, including
operator data, indexes and table space. It stops new demo ingestion at the threshold;
it is not a hard filesystem reservation and does not stop existing deliveries or
operator writes. Deleting rows makes space reusable but may not immediately reduce
the physical size. Investigate database growth before changing the limit; never
remove the database volume to clear a quota.

Every quota rejection returns sanitized HTTP 429 with `Retry-After`: the remaining
rolling window or UTC day, or 60 seconds for active/storage capacity. The latter
is a polling suggestion, not a promise that capacity will be available then.

Status reads, including wrong/missing tokens and unknown valid UUIDs, are limited
to 60 requests per client and 600 globally per UTC clock minute. Fixed windows can
allow a burst at their boundary. They support the page's two-second polling.
Counters are transactional and restart-persistent. A global cap bounds the number
of new client buckets; old buckets are deleted on polling and worker cleanup.
Malformed requests rejected by routing/validation are not counted as status reads.
These are application workload limits, not network-level denial-of-service protection.

## Client address and proxies

The packaged API starts Uvicorn with `--no-proxy-headers`, so the application sees the
real TCP peer. By default that peer is the client identity; `X-Forwarded-For` and
`X-Real-IP` are never trusted. Clients behind one NAT/proxy share its quota. Client
identifiers are HMAC-SHA256 digests; raw addresses are not saved in quota records.
IPv6 clients are keyed by their /64 prefix, and IPv4-mapped IPv6 addresses by the IPv4
address.

When the API is reachable only through a Cloudflare tunnel, set
`TRUST_CF_CONNECTING_IP=true`. The application then uses the `CF-Connecting-IP` header
if exactly one valid IP is present; a missing, repeated or invalid header falls back
to the socket peer (the request is still served). Do not enable it while the API port
is directly reachable, because any client could then choose its own identity.

## HTTP edge

Every response carries a restrictive `Content-Security-Policy`, `X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` and `Cross-Origin-*-Policy`
headers; `/v1/*`, `/demo/*` and `/health/*` are `Cache-Control: no-store`. A header
set by a route (the demo page's stricter CSP) is not overridden. `/docs` (Swagger UI,
loaded from jsDelivr) gets a looser CSP that allows that CDN; `/redoc` is disabled.

The request body must arrive within `BODY_READ_TIMEOUT` seconds (default 10), otherwise
the API answers 408. Uvicorn runs with `--timeout-keep-alive 5` and
`--limit-concurrency 64` (excess requests receive 503). Unknown paths return 404 and
unsupported methods return 405, both as JSON errors. `HEAD` on the `/health/*`
endpoints answers like `GET` without a body; `OPTIONS` (and `HEAD` elsewhere) returns 405.

## Cleanup

The worker runs cleanup at startup and every 60 seconds while its loop is healthy.
Each transaction removes at most 100 demo events whose token expired at least
24 hours ago and whose delivery is terminal (`delivered` or `failed`). Since tokens
last one hour, normal retention is at least 25 hours after creation. Backlogs drain
in subsequent batches. Active runs remain recoverable even when their tokens expire.

Deleting the event cascades to its run, attempts and delivery, and removes the
embedded idempotency key. The same transaction deletes that synthetic demo's receiver
effect. Operator events and their durable receiver effects are never selected.
Cleanup and admission share the advisory lock. If the worker is offline or the
database is unavailable, cleanup pauses; storage admission still rejects at capacity.

Verification: `tests/test_sandbox.py` covers concurrency, quota boundaries, storage,
polling, client identity, sanitization, retention and actual worker-process cleanup.
`examples.sandbox_smoke` checks the running API without printing read tokens.
