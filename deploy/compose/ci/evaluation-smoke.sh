#!/usr/bin/env bash
# Prove an evaluation batch survives its container being stopped.
#
# "A run completes" is not the assertion -- an in-process test already shows
# that, on both backends. What has to be proved here is the pair of properties
# that only exist across a real process boundary:
#
#   1. **A different container can watch it.** The `api` replica never starts a
#      batch in this test; it only reads `/evaluation/status`, which is exactly
#      the browser's position. If progress lived in the runner's memory this
#      would see nothing at all.
#
#   2. **A killed batch is reclaimed and resumed.** The runner is stopped
#      mid-replay. The row must stop claiming to be `running` -- otherwise a UI
#      shows a spinner nobody will ever stop -- and the next attempt must pick
#      up from its checkpoints rather than replaying from zero.
#
# The kill is a real `docker compose stop`, not an exception: "the process went
# away" is not a state a mock reproduces honestly, and this repository has been
# bitten by container-only behaviour four separate times.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "${HERE}/docker-compose.evaluation.yml")
PSQL=("${COMPOSE[@]}" exec -T postgres psql -qtAX -U pheasant -d pheasant -c)
API="http://127.0.0.1:8770"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
query() { "${PSQL[@]}" "$1" | tr -d '[:space:]'; }
status_field() { curl -fsS "${API}/evaluation/status" | python3 -c "import json,sys;print(json.load(sys.stdin).get('$1',''))"; }

await() {
  # $1 label, $2 SQL, $3 seconds. Succeeds on the first non-zero value.
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
  echo "FAILED: ${label} never became non-zero within ${seconds}s" >&2
  return 1
}

await_status() {
  # $1 expected status, $2 seconds
  local want="$1" seconds="$2" waited=0 got
  while [ "${waited}" -lt "${seconds}" ]; do
    got="$(status_field status || true)"
    if [ "${got}" = "${want}" ]; then
      echo "    /evaluation/status: ${got}"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "FAILED: /evaluation/status stayed '${got}', never reached '${want}' in ${seconds}s" >&2
  return 1
}

log "Bringing up postgres, the migrator and the api replica"
"${COMPOSE[@]}" up -d --wait --wait-timeout 240 api

log "Nothing has run yet, and the api says so rather than guessing"
test "$(status_field status)" = "none"
test "$(status_field enabled)" = "True"

log "Phase 1: start a batch and stop its container mid-replay"
# 3s per (cohort, variant) pair against a 36-pair matrix is ~108s of work, so
# there is a comfortable window in which to stop it. The seed runs first and is
# fast.
"${COMPOSE[@]}" run -d --name pheasant-eval-runner \
  -e PHEASANT_EVAL_SEED=1 -e PHEASANT_EVAL_STALL=3 runner

# The api must see the batch it did not start.
await "run rows" "SELECT count(*) FROM evaluation_runs;" 120
await_status running 120
await "checkpointed replays" "SELECT count(*) FROM evaluation_replays;" 120

PARTIAL="$(query 'SELECT max(completed_units) FROM evaluation_runs;')"
TOTAL="$(query 'SELECT max(total_units) FROM evaluation_runs;')"
PHASE="$(status_field phase)"
echo "    watched from another container: phase=${PHASE} ${PARTIAL}/${TOTAL} replays"
test "${PHASE}" = "replay"
test "${TOTAL}" != "0"

log "Killing the runner, the way a stopped container dies"
docker stop -t 0 pheasant-eval-runner >/dev/null
docker rm -f pheasant-eval-runner >/dev/null

log "The row still says 'running' — nothing has rewritten it yet"
test "$(query "SELECT count(*) FROM evaluation_runs WHERE status='running';")" = "1"
CHECKPOINTS="$(query 'SELECT count(*) FROM evaluation_replays;')"
echo "    checkpoints kept across the kill: ${CHECKPOINTS}"
test "${CHECKPOINTS}" != "0"

log "Waiting out the heartbeat window (evaluation.run_stale_seconds: 20s)"
# Reclamation is deliberately not instant: a slow-but-healthy batch must not be
# declared dead out from under itself, so a run is only reclaimable once it has
# missed its heartbeat window. Until then the row saying `running` is *correct*
# -- the process might still be there.
sleep 25
test "$(query "SELECT count(*) FROM evaluation_runs WHERE status='running';")" = "1"
echo "    still 'running' before anything reclaims it, which is correct"

log "Restarting the api: it reclaims the dead run at boot"
# `--role api` never runs the scheduler beat, and the API is exactly where
# somebody is watching a bar that would otherwise spin forever. Reclamation at
# startup is what closes that.
"${COMPOSE[@]}" restart api
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 api
await_status interrupted 120
RECLAIMED_UNITS="$(status_field completed_units)"
echo "    interrupted after ${RECLAIMED_UNITS} of ${TOTAL} replays"
test "${RECLAIMED_UNITS}" != "0"
test -n "$(status_field error)"

log "Phase 2: run it again — it resumes rather than starting over"
"${COMPOSE[@]}" run --rm -T runner | tee /tmp/eval-resume.log
python3 - <<'PY'
import json
# The runner prints one JSON object as its last act; everything before it is
# progress. Reading the file rather than a shell variable keeps the quoting
# out of this, which is the kind of thing that breaks a smoke test for a
# reason that is not the feature.
outcome = None
for line in open("/tmp/eval-resume.log"):
    line = line.strip()
    if line.startswith("{"):
        outcome = json.loads(line)
assert outcome is not None, "the runner printed no result object"
assert outcome["status"] == "completed", outcome
# The whole point: attempt 2 reused what attempt 1 finished.
assert outcome["attempts"] == 2, outcome
assert outcome["resumed_replays"] > 0, outcome
print(
    f"    resumed {outcome['resumed_replays']} checkpointed replay(s) "
    f"on attempt {outcome['attempts']}"
)
PY

log "One run row, not two: the id is content-addressed"
test "$(query 'SELECT count(*) FROM evaluation_runs;')" = "1"
test "$(query "SELECT count(*) FROM evaluation_runs WHERE status='completed';")" = "1"

log "The checkpoints were cleared once the report was committed"
test "$(query 'SELECT count(*) FROM evaluation_replays;')" = "0"

log "The api serves the finished report, with its gates and its denominators"
curl -fsS "${API}/evaluation/report" > /tmp/eval-report.json
python3 - <<'PY'
import json
report = json.load(open("/tmp/eval-report.json"))
gates = {gate["gate_id"]: gate["passed"] for gate in report["gates"]}
assert gates, "no gates in the report"
failed = [name for name, passed in gates.items() if not passed]
assert not failed, f"gates failed in a clean CI region: {failed}"

vector = report["health_vector"]
assert vector, "no health vector"
# Every published number carries its denominator, and one that could not be
# measured is null rather than zero. This is the contract the whole plane is
# built on, asserted against a real container's real output.
for name, entry in vector.items():
    assert "status" in entry, name
    if entry["value"] is not None:
        assert entry.get("denominator") is not None, f"{name} published without a denominator"
    else:
        assert entry["status"] in {"insufficient_evidence", "not_applicable"}, (name, entry)

coverage = report["evidence_coverage"]
assert coverage["evidenced_queries"] > 0, "no query carried outcome evidence"
assert report["explanations"]["end_user"], "no end-user explanation"
assert report["run_identity"]["attempts"] == 2, report["run_identity"]
print(f"    {len(gates)} gates passed; {len(vector)} health-vector entries; "
      f"{coverage['evidenced_queries']}/{coverage['eligible_queries']} queries evidenced")
PY

log "Evaluation state is never retrievable as knowledge"
# The boundary, checked where it matters: a region must not answer a question
# with its own report.
RUN_ID="$(query 'SELECT run_id FROM evaluation_runs LIMIT 1;')"
curl -fsS -X POST "${API}/search" -H 'content-type: application/json' \
  -d "{\"query\": \"${RUN_ID}\", \"mode\": \"hybrid\", \"max_results\": 10}" > /tmp/eval-search.json
python3 - "${RUN_ID}" <<'PY'
import json, sys
run_id = sys.argv[1]
results = json.load(open("/tmp/eval-search.json")).get("results", [])
leaked = [r for r in results if run_id and run_id in json.dumps(r, default=str)]
assert not leaked, f"the region returned its own evaluation run as a search result: {leaked}"
print(f"    searched for the run id across {len(results)} results; nothing leaked")
PY

log "The evaluation plane survives a stopped container"
