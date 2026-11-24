"""Container health probe: exit 0 only if the worker heartbeat is fresh.

Uses psycopg directly (no ORM or settings imports) so the probe starts quickly
inside a small, read-only container.
"""

import os
import sys

import psycopg

MAX_AGE_SECONDS = 30


def main() -> int:
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, connect_timeout=3, options="-c statement_timeout=3000") as conn:
        row = conn.execute(
            "SELECT max(last_seen_at) > now() - make_interval(secs => %s) FROM worker_heartbeat",
            (MAX_AGE_SECONDS,),
        ).fetchone()
    return 0 if row and row[0] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
