#!/usr/bin/env bash
# Logical backup of the relay database: pg_dump custom format, streamed from the db container
# to backups/relay-<UTC timestamp>.dump. The dump is written to a .partial file and only renamed
# after it passes a pg_restore --list check, so a failed dump never counts as a backup.
# Only the newest KEEP backups are retained. Data is never printed.
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP="${BACKUP_KEEP:-7}"
DIR="${BACKUP_DIR:-backups}"

if ! docker compose ps --status running --services 2>/dev/null | grep -qx db; then
  echo "backup: the db service is not running (start it with: make up)" >&2
  exit 1
fi

umask 077
mkdir -p "$DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$DIR/relay-$stamp.dump"
while [ -e "$final" ]; do # two backups within one second: wait for a new timestamp
  sleep 1
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  final="$DIR/relay-$stamp.dump"
done
partial="$final.partial"
trap 'rm -f "$partial"' EXIT

docker compose exec -T db pg_dump -Fc -U relay relay >"$partial"
if ! docker compose exec -T db pg_restore --list <"$partial" >/dev/null; then
  echo "backup: the dump is not readable; discarded" >&2
  exit 1
fi
mv "$partial" "$final"

# Prune: keep the newest $KEEP dumps (timestamps sort lexicographically).
mapfile -t dumps < <(find "$DIR" -maxdepth 1 -name 'relay-*.dump' -printf '%f\n' | sort -r)
for old in "${dumps[@]:$KEEP}"; do
  rm -f -- "$DIR/$old"
done

echo "backup: wrote $final ($(du -h "$final" | cut -f1))"
