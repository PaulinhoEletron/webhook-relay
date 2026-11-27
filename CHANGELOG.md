# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased (v1.0.0)

### Added

- Authenticated event ingestion with idempotency keys, enforced by a unique database constraint;
  status and attempt inspection endpoints; consistent JSON errors.
- Separate worker with PostgreSQL-backed claiming (`SKIP LOCKED`), leases with token fencing, crash
  recovery, bounded retries (five attempts, 5/15/45/120 s with jitter, `Retry-After` support) and
  HMAC-signed outgoing requests.
- Example receiver with signature verification and durable deduplication by event ID.
- Public sandbox with three scenarios, per-run bearer tokens, PostgreSQL-enforced quotas, storage cap and
  retention cleanup; a browser delivery lab served by the API.
- HTTP edge hardening: security headers, body size and read-time limits, client identity rules for
  Cloudflare tunnels, JSON 404/405 responses.
- JSON structured logs and Prometheus metrics on internal listeners.
- Container hardening and a pinned build supply chain; resource limits on every service.
- `make backup`, `make restore`, `make rollback` and an operations runbook with measured idle memory.
- End-to-end script (`make e2e`), Playwright browser checks (`make test-browser`) and a GitHub Actions
  pipeline (lint, tests, migrations, security scans, image scan, end-to-end).
- README, architecture notes and decision records.
