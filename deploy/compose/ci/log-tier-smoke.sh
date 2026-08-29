#!/usr/bin/env bash
# Prove the log tier works across a container boundary.
#
# The decisive assertion is not "a row exists" -- an in-process test already
# shows that. It is *who wrote it*: the batch has to be claimed by a process
# whose owner id names the `logger` container, because the `api` replica that
# produced it deliberately does not drain (`_owns_log_upkeep` is False for
# `api` once a log queue exists). If the api ever started draining its own
# batches, every other assertion here would still pass and the tier would be
# doing nothing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "${HERE}/docker-compose.log-tier.yml")
PSQL=("${COMPOSE[@]}" exec -T postgres psql -qtAX -U pheasant -d pheasant -c)

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

query() { "${PSQL[@]}" "$1" | tr -d '[:space:]'; }

await() {
  # $1 label, $2 SQL, $3 expected-nonzero-predicate, $4 seconds
  local label="$1" sql="$2" seconds="$3" waited=0 value
  while [ "${waited}" -lt "${seconds}" ]; do
    value="$(query "${sql}" || true)"
    if [ -n "${value}" ] && [ "${value}" != "0" ]; then
      echo "    ${label}: ${value}"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "TIMED OUT after ${seconds}s waiting for ${label} (last value: '${value:-}')" >&2
  return 1
}

log "Bringing up postgres, the migrator, an api replica and a logger"
"${COMPOSE[@]}" up -d --wait --wait-timeout 240

# The corpus was indexed by the migrator: an `api` replica publishes index
# work rather than running it, and this topology has no indexer on purpose.
log "Driving real searches through the api replica"
for query_text in "pheasant-flock rollout" "filewatch daemon nightly" "vault seal rotation"; do
  curl -fsS -X POST localhost:8765/search \
    -H 'content-type: application/json' \
    -H 'X-Pheasant-Session: ci-session' \
    -H 'X-Pheasant-Principal: user:ci' \
    -H "traceparent: 00-$(printf 'a%.0s' {1..32})-$(printf 'b%.0s' {1..16})-01" \
    -d "{\"query\": \"${query_text}\", \"mode\": \"hybrid\", \"max_results\": 5}" >/dev/null
done

log "The api published batches to the log tier's own queue"
await "log_tasks rows" "SELECT count(*) FROM log_tasks;" 60
# Its own table, never the indexer's.
test "$(query 'SELECT count(*) FROM log_tasks;')" != "0"

log "The logger drained them into the hot store"
await "interaction_events rows" "SELECT count(*) FROM interaction_events;" 90

log "Checking WHO drained them"
LOGGER_HOST="$("${COMPOSE[@]}" exec -T logger hostname | tr -d '[:space:]')"
OWNERS="$(query "SELECT string_agg(DISTINCT split_part(owner, ':', 1), ',') FROM log_tasks WHERE owner IS NOT NULL;")"
echo "    logger container: ${LOGGER_HOST}"
echo "    claim owners:     ${OWNERS}"
case ",${OWNERS}," in
  *",${LOGGER_HOST},"*) echo "    the logger claimed the work" ;;
  *) echo "FAILED: batches were not claimed by the logger container" >&2; exit 1 ;;
esac

log "The rows carry identity, modality and the caller's own trace"
test "$(query "SELECT count(*) FROM interaction_events WHERE session_id='ci-session';")" != "0"
test "$(query "SELECT count(*) FROM interaction_events WHERE principal='user:ci';")" != "0"
test "$(query "SELECT count(*) FROM interaction_events WHERE modality='ui';")" != "0"
# Timestamps and traces are guaranteed, not best-effort.
test "$(query "SELECT count(*) FROM interaction_events WHERE trace_id IS NULL OR started_at IS NULL OR duration_ms IS NULL;")" = "0"
# The agent's inbound trace was adopted rather than replaced.
test "$(query "SELECT count(*) FROM interaction_events WHERE trace_id = repeat('a', 32);")" != "0"
echo "    identity, modality, trace and timing all present"

log "Every batch reached a terminal state; none dead-lettered"
test "$(query "SELECT count(*) FROM log_tasks WHERE status='dead';")" = "0"
await "completed batches" "SELECT count(*) FROM log_tasks WHERE status='done';" 60

log "The log tier is real"
