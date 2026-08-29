#!/usr/bin/env bash
# Prove the log tier works across a container boundary.
#
# "A row exists" is not the assertion -- an in-process test already shows that.
# What has to be proved is that a *separate process* does the draining, because
# if the producer ever started draining its own batches every other check here
# would still pass while the tier did nothing.
#
# So the run is split in two phases:
#
#   1. api only, no logger. Batches must pile up in `log_tasks` and
#      `interaction_events` must stay **empty** -- the producer does not drain.
#   2. start the logger. The rows must appear.
#
# Phase 1 is the half that can fail silently, and it is the reason for the
# split. An earlier version of this asserted on `log_tasks.owner` instead and
# failed for a reason that was not a bug: `LocalQueue.ack` sets `owner=NULL`,
# so a batch that has been drained *successfully* has no owner left to check.
# The column is a claim lease, not an audit trail.
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

log "Phase 1: postgres, the migrator and an api replica -- deliberately no logger"
"${COMPOSE[@]}" up -d --wait --wait-timeout 240 postgres db-init api

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
test "$(query 'SELECT count(*) FROM index_tasks;')" = "0"

log "...and did NOT drain them itself"
# The half that can fail silently. Give it long enough that a producer which
# *did* drain would have finished: the flush interval is 1s and there is no
# scheduler beat at all in this config.
sleep 10
DRAINED_WITHOUT_LOGGER="$(query 'SELECT count(*) FROM interaction_events;')"
PENDING="$(query "SELECT count(*) FROM log_tasks WHERE status IN ('pending','inflight');")"
echo "    interaction_events without a logger: ${DRAINED_WITHOUT_LOGGER}"
echo "    batches still waiting:               ${PENDING}"
if [ "${DRAINED_WITHOUT_LOGGER}" != "0" ]; then
  echo "FAILED: the api drained its own batches; the log tier would be doing nothing" >&2
  exit 1
fi
test "${PENDING}" != "0"

log "Phase 2: starting the logger"
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 logger

log "The logger drained them into the hot store"
await "interaction_events rows" "SELECT count(*) FROM interaction_events;" 90

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
# Nothing left waiting, so the logger cleared the backlog phase 1 built.
test "$(query "SELECT count(*) FROM log_tasks WHERE status IN ('pending','inflight');")" = "0"

log "The log tier is real"
