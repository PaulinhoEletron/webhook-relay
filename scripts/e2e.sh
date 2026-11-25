#!/usr/bin/env bash
# End-to-end check of the running Compose stack. The Python smoke modules in examples/ run
# inside the api container (which already holds the operator key) and are the building
# blocks; this script orchestrates them, adds black-box curl checks from the host, restarts
# the stateful services and prints a PASS/FAIL table. Secrets are never printed.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${RELAY_API_URL:-http://127.0.0.1:8000}"
READY_TIMEOUT="${E2E_READY_TIMEOUT:-90}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT
NAMES=() RESULTS=() FAILED=0

api_py() { docker compose exec -T api python "$@"; }

# record NAME HINT CMD...: run CMD, keep its output, and note PASS/FAIL (never aborts).
record() {
  local name="$1" hint="$2"
  shift 2
  if "$@" >"$OUT" 2>&1; then
    NAMES+=("$name") RESULTS+=(PASS)
  else
    NAMES+=("$name") RESULTS+=(FAIL)
    FAILED=1
    echo "--- FAIL: $name: $hint" >&2
    tail -n 12 "$OUT" >&2
  fi
}

# wait_ready: bounded wait for API readiness and an online worker.
wait_ready() {
  local i
  for ((i = 0; i < READY_TIMEOUT; i++)); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/health/ready" || true)" = 200 ] &&
      curl -s "$BASE_URL/health/worker" | grep -q '"online"'; then
      return 0
    fi
    sleep 1
  done
  echo "stack not ready within ${READY_TIMEOUT}s (check: docker compose ps, docker compose logs api worker)" >&2
  return 1
}

# status_is URL EXPECTED [curl args...]
status_is() {
  local url="$1" want="$2" got
  shift 2
  got="$(curl -s -o /dev/null -w '%{http_code}' "$@" "$url" || true)"
  [ "$got" = "$want" ] || { echo "expected HTTP $want from $url, got $got" >&2; return 1; }
}

oversize_post() {
  head -c 16385 /dev/zero | tr '\0' x |
    curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-API-Key: invalid' \
      -H 'Content-Type: application/json' -H 'Idempotency-Key: e2e-oversize' \
      --data-binary @- "$BASE_URL/v1/events" | grep -qx 413
}

[ -f .env ] || { echo "missing .env: run make init" >&2; exit 2; }

make up
record "stack ready" "API or worker did not become ready" wait_ready
[ "${RESULTS[0]}" = PASS ] || { printf 'FAIL  stack ready\n'; exit 1; }

record "producer delivers" "producer event not delivered within 30 s; check worker logs and DESTINATION_URL" \
  api_py examples/producer.py
EVENT_ID="$(sed -n 's/^Accepted \(.*\)$/\1/p' "$OUT" | head -n 1)"
record "operator API (replay, conflict, auth, limits)" "regression smoke failed; event not delivered or API contract broken" \
  api_py -m examples.regression_smoke
record "demo scenarios 200 / 503,503,200 / 400" "demo scenario attempts did not match" \
  api_py -m examples.demo_smoke
record "bad API key -> 401" "expected 401" \
  status_is "$BASE_URL/v1/events/00000000-0000-4000-8000-000000000000" 401 -H 'X-API-Key: invalid'
record "oversize body -> 413" "expected 413" oversize_post
record "/metrics not public -> 404" "metrics must only be served on the internal listener" \
  status_is "$BASE_URL/metrics" 404

docker compose restart api worker db >/dev/null 2>&1 || true
record "stack ready after restart" "stack did not recover after restart" wait_ready
if [ -n "$EVENT_ID" ]; then
  record "event still delivered after restart" "event $EVENT_ID not delivered after restart" \
    api_py -m examples.regression_smoke --event-id "$EVENT_ID"
else
  NAMES+=("event still delivered after restart") RESULTS+=(FAIL) FAILED=1
  echo "--- FAIL: no producer event id to recheck" >&2
fi

echo
for i in "${!NAMES[@]}"; do printf '%-5s %s\n' "${RESULTS[$i]}" "${NAMES[$i]}"; done
if [ "$FAILED" -eq 0 ]; then echo "e2e: PASS"; else echo "e2e: FAIL" >&2; exit 1; fi
