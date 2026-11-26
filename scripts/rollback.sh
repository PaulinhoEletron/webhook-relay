#!/usr/bin/env bash
# Run an older version of the service against the existing data.
# Usage: scripts/rollback.sh REF   (or: make rollback VERSION=<git tag or commit>)
#
# The working tree is never touched: the ref is exported with `git archive` into a temporary
# directory and built from its own Dockerfile and compose.yaml, with the same Compose project
# (so the data volume is shared) and an image tagged with the ref (the current tag is kept).
# If the database revision is newer than the target's newest migration, the schema is first
# downgraded with the CURRENT image, which holds the downgrade scripts. A backup is taken first.
# Roll forward again with `make up` from the current checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
root="$(pwd)"

ref="${1:-${VERSION:-}}"
if [ -z "$ref" ]; then
  echo "rollback: pass a git tag or commit: make rollback VERSION=<ref>" >&2
  exit 2
fi
if ! sha="$(git rev-parse --verify --short "$ref^{commit}" 2>/dev/null)"; then
  echo "rollback: $ref is not a known git tag or commit" >&2
  exit 2
fi
if [ ! -f .env ]; then
  echo "rollback: .env is missing (run make init)" >&2
  exit 2
fi

if [ "${CONFIRM:-}" != yes ]; then
  if [ ! -t 0 ]; then
    echo "rollback: this replaces the running version and may downgrade the schema; run interactively or pass CONFIRM=yes. Aborted." >&2
    exit 1
  fi
  read -r -p "Roll back to $ref ($sha)? A backup is taken first. Type 'yes' to continue: " answer
  [ "$answer" = yes ] || { echo "rollback: aborted" >&2; exit 1; }
fi

echo "rollback: taking a backup first"
scripts/backup.sh

tree="$(mktemp -d)"
trap 'rm -rf "$tree"' EXIT
git archive "$ref" | tar -x -C "$tree"
install -m 600 .env "$tree/.env"

# Newest migration of the target: the revision no other migration lists as its parent.
target_head="$(
  cat "$tree"/migrations/versions/*.py | awk -F'"' '
    /^revision = /      { rev[$2] = 1 }
    /^down_revision = / { if ($2 != "") parent[$2] = 1 }
    END { for (r in rev) if (!(r in parent)) print r }'
)"
if [ -z "$target_head" ] || [ "$(wc -w <<<"$target_head")" -ne 1 ]; then
  echo "rollback: cannot determine the migration head of $ref" >&2
  exit 1
fi

current="$(docker compose exec -T db psql -U relay -d relay -tAc 'SELECT version_num FROM alembic_version')"
echo "rollback: database revision ${current:-none}, target head $target_head"
if [ -n "$current" ] && ! grep -qs "^revision = \"$current\"" "$tree"/migrations/versions/*.py; then
  echo "rollback: downgrading the schema to $target_head with the current image"
  docker compose stop api worker receiver
  docker compose run --rm migrate alembic downgrade "$target_head"
fi

echo "rollback: building and starting $ref"
cd "$tree"
export RELAY_VERSION="rollback-$sha" GIT_SHA="$sha"
# Build the shared image once, then start: building it per service in parallel races on a new tag.
docker compose build api
docker compose up -d --wait
cd "$root"
echo "rollback: $ref ($sha) is running. Return to the current version with: make up"
