"""Append-only persistence for the evaluation plane.

A thin, deliberate layer over :class:`~pheasant.persistence.state_store.StateStore`
rather than a second storage abstraction. Three properties it has to keep, each
of which has cost this codebase time somewhere else:

**Portable SQL only.** ``INSERT ... ON CONFLICT DO NOTHING``, never
``INSERT OR IGNORE``; ``?`` placeholders that the backend translates. The
SQLite-only spelling is a bug this repository has already shipped and fixed
once, and it did not surface in the offline suite -- it surfaced against a
real Postgres server. Every statement here is written the portable way from
the start.

**Writes are idempotent because ids are content digests.** Re-running a batch
over an unchanged snapshot with unchanged configuration re-derives the same
run id and the same metric row ids, so a retried or duplicated run is a no-op
rather than a second copy of the same numbers. That is the reproducibility
acceptance criterion made mechanical instead of aspirational.

**Nothing here is ever deleted.** Retention is a policy applied by pruning old
*runs*, not by rewriting them: the specification's audit requirement is that an
aggregate resolve to the per-query operands that produced it, and an
after-the-fact edit breaks that for every report already published.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pheasant.evaluation.contracts import (
    Cohort,
    EvaluatedQuery,
    MetricResult,
    Proof,
    SnapshotManifest,
    digest,
)

logger = logging.getLogger(__name__)


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------
# snapshots


def save_snapshot(state: Any, manifest: SnapshotManifest) -> str:
    state.execute(
        "INSERT INTO evaluation_snapshots"
        "(snapshot_id, kb_id, created_at, effective_as_of, complete, manifest_json) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT (snapshot_id) DO NOTHING",
        (
            manifest.snapshot_id,
            manifest.kb_id,
            manifest.created_at,
            manifest.effective_as_of,
            1 if manifest.complete else 0,
            _json(manifest.as_dict()),
        ),
    )
    return manifest.snapshot_id


def load_snapshot(state: Any, snapshot_id: str) -> SnapshotManifest | None:
    rows = state.rows(
        "SELECT manifest_json FROM evaluation_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    )
    if not rows:
        return None
    return SnapshotManifest.from_dict(json.loads(rows[0]["manifest_json"]))


def list_snapshots(state: Any, kb_id: str, *, limit: int = 50) -> list[SnapshotManifest]:
    rows = state.rows(
        "SELECT manifest_json FROM evaluation_snapshots WHERE kb_id=? "
        "ORDER BY created_at DESC, snapshot_id DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [SnapshotManifest.from_dict(json.loads(row["manifest_json"])) for row in rows]


def previous_snapshot(
    state: Any, kb_id: str, *, before: str, exclude: str = ""
) -> SnapshotManifest | None:
    """The most recent snapshot strictly older than ``before``.

    What longitudinal deltas and rank churn are measured against. Excluding
    the current snapshot by id as well as by time matters: two snapshots
    created within the same second are ordered by id, and comparing a snapshot
    to itself reports a churn of zero that means nothing.
    """

    rows = state.rows(
        "SELECT manifest_json FROM evaluation_snapshots "
        "WHERE kb_id=? AND created_at<=? AND snapshot_id<>? "
        "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1",
        (kb_id, before, exclude),
    )
    if not rows:
        return None
    return SnapshotManifest.from_dict(json.loads(rows[0]["manifest_json"]))


# --------------------------------------------------------------------------
# proof


def save_proofs(state: Any, proofs: list[Proof]) -> int:
    """Insert proof rows, skipping any that already exist.

    Written one statement at a time rather than as one batch insert, and that
    is deliberate: a batch runs in a single transaction, so one malformed row
    rolls back every good row beside it. The interaction ledger learned this
    the expensive way (one null ``trace_id`` cost a batch of hundreds), and a
    proof arrives from an HTTP body, which is a strictly more hostile source
    than a spool file.
    """

    written = 0
    for proof in proofs:
        try:
            state.execute(
                "INSERT INTO evaluation_proofs(proof_id, kb_id, query_id, target_type, "
                "target_id, event_type, polarity, strength, weight, observed_at, "
                "interaction_id, snapshot_id, principal_partition, position, exposed, "
                "outcome_reference, supersedes_proof_id, reason_code, multipliers_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (proof_id) DO NOTHING",
                (
                    proof.proof_id,
                    proof.kb_id,
                    proof.query_id,
                    proof.target_type,
                    proof.target_id,
                    proof.event_type,
                    proof.polarity,
                    proof.strength,
                    float(proof.weight),
                    proof.observed_at,
                    proof.interaction_id,
                    proof.snapshot_id,
                    proof.principal_partition,
                    proof.position,
                    1 if proof.exposed else 0,
                    proof.outcome_reference,
                    proof.supersedes_proof_id,
                    proof.reason_code,
                    _json(proof.multipliers),
                ),
            )
            written += 1
        except Exception:  # noqa: BLE001 - one bad row must not cost the rest
            logger.warning("evaluation: rejected malformed proof %s", proof.proof_id, exc_info=True)
    return written


def load_proofs(
    state: Any,
    kb_id: str,
    *,
    query_ids: list[str] | None = None,
    before: str | None = None,
    limit: int = 100_000,
) -> list[Proof]:
    """Proof rows for a knowledge base, optionally narrowed and time-capped.

    ``before`` is the leakage control, and it is not optional in practice: a
    replay reconstructing what the region could have known at ``t`` must not
    read evidence recorded after ``t``. Filtering here rather than at the
    metric means every metric inherits the guarantee instead of each one
    having to remember it.
    """

    clauses = ["kb_id=?"]
    params: list[Any] = [kb_id]
    if before:
        clauses.append("observed_at<=?")
        params.append(before)
    if query_ids is not None:
        if not query_ids:
            return []
        # Chunked so a large cohort cannot exceed a driver's parameter limit
        # (SQLite's default ceiling is 999, and a rolling cohort can be
        # larger than that).
        out: list[Proof] = []
        for start in range(0, len(query_ids), 400):
            batch = query_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = state.rows(
                f"SELECT * FROM evaluation_proofs WHERE {' AND '.join(clauses)} "
                f"AND query_id IN ({placeholders}) ORDER BY observed_at, proof_id LIMIT ?",
                (*params, *batch, int(limit)),
            )
            out.extend(Proof.from_row(row) for row in rows)
        return out
    rows = state.rows(
        f"SELECT * FROM evaluation_proofs WHERE {' AND '.join(clauses)} "
        "ORDER BY observed_at, proof_id LIMIT ?",
        (*params, int(limit)),
    )
    return [Proof.from_row(row) for row in rows]


# --------------------------------------------------------------------------
# cohorts


def save_cohort(state: Any, cohort: Cohort) -> str:
    """Persist a cohort. A frozen one is written once and never rewritten.

    The ``DO NOTHING`` is the freeze: an anchor cohort's id is a digest of its
    query set, so re-materializing an unchanged anchor is a no-op, and a
    *changed* anchor gets a new id rather than silently replacing the one every
    past trend point was measured against.
    """

    state.execute(
        "INSERT INTO evaluation_cohorts(cohort_id, kb_id, name, purpose, created_at, "
        "frozen, window_start, window_end, eligibility_digest, query_count, queries_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (cohort_id) DO NOTHING",
        (
            cohort.cohort_id,
            cohort.kb_id,
            cohort.name,
            cohort.purpose,
            cohort.created_at,
            1 if cohort.frozen else 0,
            cohort.window_start,
            cohort.window_end,
            cohort.eligibility_digest,
            cohort.query_count,
            _json([q.as_dict() for q in cohort.queries]),
        ),
    )
    return cohort.cohort_id


def _cohort_from_row(row: Any) -> Cohort:
    data = dict(row)
    queries = tuple(
        EvaluatedQuery(
            **{k: v for k, v in item.items() if k in EvaluatedQuery.__dataclass_fields__}
        )
        for item in json.loads(data.get("queries_json") or "[]")
    )
    return Cohort(
        cohort_id=data["cohort_id"],
        kb_id=data["kb_id"],
        name=data["name"],
        purpose=data["purpose"],
        queries=queries,
        created_at=data.get("created_at") or "",
        frozen=bool(data.get("frozen")),
        window_start=data.get("window_start"),
        window_end=data.get("window_end"),
        eligibility_digest=data.get("eligibility_digest") or "",
    )


def load_cohort(state: Any, cohort_id: str) -> Cohort | None:
    rows = state.rows("SELECT * FROM evaluation_cohorts WHERE cohort_id=?", (cohort_id,))
    return _cohort_from_row(rows[0]) if rows else None


def find_cohort(state: Any, kb_id: str, purpose: str, name: str) -> Cohort | None:
    """The newest cohort with this purpose and name.

    How a frozen anchor is *re-found* on the next run rather than rebuilt from
    a corpus that has moved on. Without this an "anchor" would be re-derived
    every run and would not be an anchor at all.
    """

    rows = state.rows(
        "SELECT * FROM evaluation_cohorts WHERE kb_id=? AND purpose=? AND name=? "
        "ORDER BY created_at DESC, cohort_id DESC LIMIT 1",
        (kb_id, purpose, name),
    )
    return _cohort_from_row(rows[0]) if rows else None


def list_cohorts(state: Any, kb_id: str, *, limit: int = 100) -> list[Cohort]:
    rows = state.rows(
        "SELECT * FROM evaluation_cohorts WHERE kb_id=? ORDER BY created_at DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [_cohort_from_row(row) for row in rows]


# --------------------------------------------------------------------------
# runs and metrics


def open_run(
    state: Any,
    *,
    run_id: str,
    kb_id: str,
    snapshot_id: str,
    started_at: str,
    mode: str,
    config_digest: str,
) -> None:
    state.execute(
        "INSERT INTO evaluation_runs(run_id, kb_id, snapshot_id, started_at, status, "
        "mode, config_digest) VALUES(?,?,?,?,?,?,?) ON CONFLICT (run_id) DO NOTHING",
        (run_id, kb_id, snapshot_id, started_at, "running", mode, config_digest),
    )


def close_run(
    state: Any,
    *,
    run_id: str,
    finished_at: str,
    status: str,
    gates_passed: bool,
    report: dict[str, Any],
) -> None:
    state.execute(
        "UPDATE evaluation_runs SET finished_at=?, status=?, gates_passed=?, report_json=? "
        "WHERE run_id=?",
        (finished_at, status, 1 if gates_passed else 0, _json(report), run_id),
    )


def save_metrics(state: Any, run_id: str, kb_id: str, results: list[MetricResult]) -> int:
    written = 0
    for result in results:
        row_id = digest(
            run_id,
            result.metric_id,
            result.scope.cohort_id,
            result.scope.variant_id,
            result.scope.query_id,
        )
        try:
            state.execute(
                "INSERT INTO evaluation_metrics(id, run_id, kb_id, metric_id, metric_version, "
                "classification, snapshot_id, cohort_id, variant_id, query_id, value, "
                "numerator, denominator, status, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                (
                    row_id,
                    run_id,
                    kb_id,
                    result.metric_id,
                    result.metric_version,
                    result.classification,
                    result.scope.snapshot_id,
                    result.scope.cohort_id,
                    result.scope.variant_id,
                    result.scope.query_id,
                    result.value,
                    result.numerator,
                    result.denominator,
                    result.status,
                    _json(result.as_dict()),
                ),
            )
            written += 1
        except Exception:  # noqa: BLE001 - one metric must not cost a whole run
            logger.warning("evaluation: could not persist %s", result.metric_id, exc_info=True)
    return written


def load_report(state: Any, run_id: str) -> dict[str, Any] | None:
    rows = state.rows("SELECT report_json FROM evaluation_runs WHERE run_id=?", (run_id,))
    if not rows or not rows[0]["report_json"]:
        return None
    return json.loads(rows[0]["report_json"])


def latest_run(state: Any, kb_id: str) -> dict[str, Any] | None:
    rows = state.rows(
        "SELECT run_id, snapshot_id, started_at, finished_at, status, mode, gates_passed "
        "FROM evaluation_runs WHERE kb_id=? ORDER BY started_at DESC, run_id DESC LIMIT 1",
        (kb_id,),
    )
    return dict(rows[0]) if rows else None


def list_runs(state: Any, kb_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = state.rows(
        "SELECT run_id, snapshot_id, started_at, finished_at, status, mode, gates_passed "
        "FROM evaluation_runs WHERE kb_id=? ORDER BY started_at DESC, run_id DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [dict(row) for row in rows]


def metric_trend(
    state: Any,
    kb_id: str,
    metric_id: str,
    *,
    cohort_name: str | None = None,
    variant_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """One metric's history, newest last, for longitudinal reporting.

    Joined against ``evaluation_runs`` for the snapshot time rather than
    ordering on the metric row, which carries no clock of its own -- and
    against ``evaluation_cohorts`` when a cohort *name* is asked for, because
    a frozen anchor keeps one name across many cohort ids only when it never
    changes, and the trend must survive it changing.
    """

    clauses = ["m.kb_id=?", "m.metric_id=?", "m.query_id IS NULL"]
    params: list[Any] = [kb_id, metric_id]
    if variant_id:
        clauses.append("m.variant_id=?")
        params.append(variant_id)
    if cohort_name:
        clauses.append("c.name=?")
        params.append(cohort_name)
    rows = state.rows(
        "SELECT r.started_at AS started_at, m.snapshot_id AS snapshot_id, m.run_id AS run_id, "
        "m.value AS value, m.numerator AS numerator, m.denominator AS denominator, "
        "m.status AS status, m.variant_id AS variant_id, m.cohort_id AS cohort_id "
        "FROM evaluation_metrics m "
        "JOIN evaluation_runs r ON r.run_id=m.run_id "
        "LEFT JOIN evaluation_cohorts c ON c.cohort_id=m.cohort_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY r.started_at DESC, m.id DESC LIMIT ?",
        (*params, int(limit)),
    )
    return [dict(row) for row in reversed(rows)]
