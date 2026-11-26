#!/usr/bin/env bash
# Restore the relay database from a dump made by scripts/backup.sh.
# Usage: scripts/restore.sh FILE   (or: make restore FILE=backups/relay-....dump)
# Stops api, worker and receiver, replaces the database contents (pg_restore --clean), then starts
# the stack again and waits until every service is healthy. Asks for confirmation unless
# CONFIRM=yes; a non-interactive shell without CONFIRM=yes aborts without touching data.
set -euo pipefail
cd "$(dirname "$0")/.."

file="${1:-${FILE:-}}"
if [ -z "$file" ]; then
  echo "restore: pass the dump file: make restore FILE=backups/relay-<timestamp>.dump" >&2
  exit 2
fi
if [ ! -f "$file" ] || [ ! -r "$file" ]; then
  echo "restore: $file does not exist or is not readable" >&2
  exit 2
fi

echo "restore: starting the db service"
docker compose up -d --wait db
if ! docker compose exec -T db pg_restore --list <"$file" >/dev/null 2>&1; then
  echo "restore: $file is not a valid pg_dump custom-format archive" >&2
  exit 2
fi

if [ "${CONFIRM:-}" != yes ]; then
  if [ ! -t 0 ]; then
    echo "restore: this REPLACES all current data; run interactively or pass CONFIRM=yes. Aborted." >&2
    exit 1
  fi
  read -r -p "This REPLACES all current relay data with $file. Type 'yes' to continue: " answer
  if [ "$answer" != yes ]; then
    echo "restore: aborted" >&2
    exit 1
  fi
fi

echo "restore: stopping api, worker and receiver"
docker compose stop api worker receiver
echo "restore: restoring $file"
if ! docker compose exec -T db pg_restore --clean --if-exists --no-owner -U relay -d relay <"$file"; then
  echo "restore: pg_restore reported errors; the stack stays stopped. Inspect the output above." >&2
  exit 1
fi
echo "restore: starting the stack"
docker compose up -d --wait
echo "restore: done"
