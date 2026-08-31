"""Hard gates: the invariants no average is allowed to absorb.

A gate is not a metric with a strict threshold. The difference is arithmetic:
metrics are combined, and anything combined can be offset. An ACL leak paired
with excellent recall produces a healthy-looking composite, and that composite
is a lie about a security failure. So gates are evaluated *before* aggregation,
they short-circuit promotion, and they are reported with the exact evidence
that failed them rather than as a score.

Four of the five default gates are checked against the **synthetic invariant
cohort**, whose cases are derived from the region's own memory records:

* ``acl_leak`` -- a scoped record must not reach a principal who did not write
  it. Checked by replaying the record's own text as a query under a principal
  that provably did not write it.
* ``stale_current_leak`` -- a superseded record must not come back under the
  default ``current_only`` policy. The stale-fact failure the memory literature
  names as the primary defect, and the one the validity model exists to stop.
* ``temporal_invariant`` -- the *same* query under ``as_of`` must bring the old
  record back. The mirror of the previous gate: a system that satisfied one by
  deleting history would fail this.
* ``abstention`` -- a query about something the corpus has never contained must
  return nothing. Cheap, and it is the case a confidently-wrong retriever fails
  first.

The fifth, ``known_positive_exclusion``, is checked against the treatment
itself: an exclusion rule that suppresses an artifact with positive proof is
removing content somebody has already demonstrated is useful, whatever else it
improves.

``incomplete_snapshot`` is a gate on the *run*, not the region: a run over a
manifest with unresolved digests cannot support a comparison, so it fails
before anything is measured.
"""

from __future__ import annotations

from typing import Any

from pheasant.evaluation.contracts import GateResult
from pheasant.evaluation.replay import VariantReplay


def _cases(cohort: Any, kind: str) -> list[Any]:
    return [q for q in cohort.queries if (q.expectation or {}).get("kind") == kind]


def _returned_record_ids(replay: VariantReplay, query_id: str) -> set[str]:
    result = replay.results.get(query_id)
    if result is None or result.failed:
        return set()
    return set(result.memory_record_ids)


def evaluate_invariants(
    cohort: Any, replay: VariantReplay, *, acl_enforced: bool = True
) -> list[GateResult]:
    """The four cohort-derived gates. Order is the report's order.

    ``acl_enforced`` mirrors ``security.acl_enforced``. With it off, a
    scope-restricted memory record is returned to any caller *by design* --
    that is the documented default, and the isolation the ACL gate asserts
    simply is not switched on. Failing the gate there would report a security
    breach on every region that never opted in, and a gate that cries wolf on
    a default configuration is a gate people learn to ignore. So it reports
    "not evaluated" instead, and says why.
    """

    gates: list[GateResult] = []

    # --- stale-current leak -------------------------------------------------
    cases = _cases(cohort, "stale_current")
    leaks = [
        case.query_id
        for case in cases
        if str(case.expectation["forbidden_record_id"])
        in _returned_record_ids(replay, case.query_id)
    ]
    gates.append(
        GateResult(
            gate_id="stale_current_leak",
            passed=not leaks,
            observed=float(len(leaks)),
            maximum=0.0,
            detail=(
                f"{len(leaks)} of {len(cases)} supersession cases returned the corrected record "
                "under the default current-only policy"
            ),
            evidence={"cases": len(cases), "failed_query_ids": leaks[:20]},
        )
    )

    # --- temporal as_of -----------------------------------------------------
    cases = _cases(cohort, "temporal_as_of")
    missing = [
        case.query_id
        for case in cases
        if str(case.expectation["expected_record_id"])
        not in _returned_record_ids(replay, case.query_id)
    ]
    gates.append(
        GateResult(
            gate_id="temporal_invariant",
            passed=not missing,
            observed=float(len(missing)),
            maximum=0.0,
            detail=(
                f"{len(missing)} of {len(cases)} as-of cases failed to bring back the record that "
                "was valid at the requested instant"
            ),
            evidence={"cases": len(cases), "failed_query_ids": missing[:20]},
        )
    )

    # --- ACL isolation ------------------------------------------------------
    cases = _cases(cohort, "acl_isolation")
    if not acl_enforced:
        gates.append(
            GateResult(
                gate_id="acl_leak",
                passed=True,
                observed=0.0,
                maximum=0.0,
                detail=(
                    "not evaluated: security.acl_enforced is off, so scope isolation is not "
                    "in force and a scoped record reaching another principal is the "
                    "documented default rather than a leak"
                ),
                evidence={"cases": len(cases), "acl_enforced": False, "status": "not_applicable"},
            )
        )
    else:
        leaked = [
            case.query_id
            for case in cases
            if str(case.expectation["forbidden_record_id"])
            in _returned_record_ids(replay, case.query_id)
        ]
        gates.append(
            GateResult(
                gate_id="acl_leak",
                passed=not leaked,
                observed=float(len(leaked)),
                maximum=0.0,
                detail=(
                    f"{len(leaked)} of {len(cases)} scoped records reached a principal that "
                    "did not write them"
                ),
                evidence={"cases": len(cases), "failed_query_ids": leaked[:20]},
            )
        )

    # --- abstention ---------------------------------------------------------
    cases = _cases(cohort, "abstention")
    answered = []
    for case in cases:
        result = replay.results.get(case.query_id)
        if result is None or result.failed:
            continue
        expected = int(case.expectation.get("expected_results", 0))
        if result.result_count > expected:
            answered.append(case.query_id)
    gates.append(
        GateResult(
            gate_id="abstention",
            passed=not answered,
            observed=float(len(answered)),
            maximum=0.0,
            detail=(
                f"{len(answered)} of {len(cases)} unanswerable queries returned results anyway"
            ),
            evidence={"cases": len(cases), "failed_query_ids": answered[:20]},
        )
    )
    return gates


def evaluate_known_positive_exclusion(
    ctx: Any, baseline: VariantReplay, treatment: VariantReplay
) -> GateResult:
    """No known-positive artifact may be removed from the results entirely.

    Stricter than the displacement *metric*, and deliberately so: displacement
    counts an artifact pushed past rank k, which is a ranking cost. This counts
    one the treatment removed from the result list altogether, which is an
    exclusion rule deleting demonstrated-useful content. The first is a
    trade-off; the second is not one anybody chose.
    """

    removed: list[dict[str, Any]] = []
    for query_id, base in baseline.results.items():
        treat = treatment.results.get(query_id)
        if base.failed or treat is None or treat.failed:
            continue
        positives = set(ctx.positives(query_id))
        if not positives:
            continue
        lost = sorted((positives & set(base.ranked_ids)) - set(treat.ranked_ids))
        if lost:
            removed.append({"query_id": query_id, "artifact_ids": lost})
    return GateResult(
        gate_id="known_positive_exclusion",
        passed=not removed,
        observed=float(sum(len(item["artifact_ids"]) for item in removed)),
        maximum=0.0,
        detail=(
            f"{len(removed)} queries lost a known-positive artifact from the result list "
            f"entirely under {treatment.variant.variant_id}"
        ),
        evidence={"queries": removed[:20]},
    )


def evaluate_snapshot(manifest: Any, *, blocking: bool = True) -> GateResult:
    """A run over an incomplete manifest cannot support a comparison."""

    incomplete = list(manifest.incomplete)
    return GateResult(
        gate_id="snapshot_complete",
        passed=not incomplete or not blocking,
        observed=float(len(incomplete)),
        maximum=0.0,
        detail=(
            "snapshot manifest is complete"
            if not incomplete
            else f"unresolved manifest sections: {', '.join(incomplete)}"
        ),
        evidence={"incomplete_sections": incomplete, "blocking": blocking},
    )


def evaluate_negative_exposure_increase(
    baseline_metric: Any, treatment_metric: Any, *, tolerance: float = 0.0
) -> GateResult:
    """Known-negative exposure must not rise past tolerance.

    An intervention that improves ranking while serving more known-bad content
    has moved the cost rather than removed it, and the mean of the two looks
    like progress.
    """

    before = getattr(baseline_metric, "value", None)
    after = getattr(treatment_metric, "value", None)
    if before is None or after is None:
        return GateResult(
            gate_id="negative_exposure_increase",
            passed=True,
            observed=0.0,
            maximum=tolerance,
            detail="not evaluated: no query in this cohort has negative-proof artifacts",
            evidence={"status": "insufficient_evidence"},
        )
    delta = round(after - before, 6)
    return GateResult(
        gate_id="negative_exposure_increase",
        passed=delta <= tolerance,
        observed=delta,
        maximum=tolerance,
        detail=f"known-negative exposure moved {delta:+.4f} (tolerance {tolerance})",
        evidence={"baseline": before, "treatment": after},
    )


def evaluate_control_regression(metric: Any, *, tolerance: float = 0.0) -> GateResult:
    """The control cohort's regression rate, as a gate rather than a score."""

    value = getattr(metric, "value", None)
    if value is None:
        return GateResult(
            gate_id="control_regression",
            passed=True,
            observed=0.0,
            maximum=tolerance,
            detail="not evaluated: the control cohort is empty",
            evidence={"status": "insufficient_evidence"},
        )
    return GateResult(
        gate_id="control_regression",
        passed=value <= tolerance,
        observed=float(value),
        maximum=tolerance,
        detail=f"{value:.1%} of control queries regressed (tolerance {tolerance:.1%})",
        evidence=dict(getattr(metric, "operands", {}) or {}),
    )


def all_passed(gates: list[GateResult]) -> bool:
    return all(gate.passed for gate in gates)


def failures(gates: list[GateResult]) -> list[GateResult]:
    return [gate for gate in gates if not gate.passed]
