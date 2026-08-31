"""One result, three readers.

The end-user summary, the agent-readable result and the developer trace are
projections of the *same* record -- never three separately-computed things.
That is a correctness property, not a tidiness one: the moment the prose
summary is computed independently of the numbers, it can disagree with them,
and the summary is the part people actually read.

What each reader gets is different because what each can act on is different:

* **A person** needs the change, the two things being compared, the concrete
  numerator and denominator, what evidence backs it, which memory or rule is
  responsible, whether the measurement is demonstrated or diagnostic, and one
  material limitation. Anything more and the limitation stops being read.
* **An agent** needs machine-checkable status, evidence sufficiency, the
  deltas, proof references, the limits of what it may conclude, and -- because
  it will otherwise invent them -- the actions it is allowed to take next.
* **A developer** needs the operands: which queries, which ranks, which proof
  events, which formula with what substituted, what was excluded and why, and
  the worst regressions rather than the mean.

The health vector at the top is deliberately a *vector*. There is no default
scalar, because the one thing a single number called "accuracy" reliably does
is get quoted without its denominator.
"""

from __future__ import annotations

from typing import Any

from pheasant.evaluation.contracts import (
    Classification,
    GateResult,
    MetricResult,
    MetricStatus,
)

#: Metrics promoted into the health vector, and the name each takes there.
#: A curated list rather than "everything demonstrated": the vector is meant to
#: be read whole, and one that grows with every added metric stops being read
#: at all.
HEALTH_VECTOR: tuple[tuple[str, str], ...] = (
    ("query_evidence_coverage", "evidence_coverage"),
    ("known_positive_recall_at_5", "known_positive_retrieval_at_5"),
    ("known_positive_reciprocal_rank", "known_positive_reciprocal_rank"),
    ("negative_exposure_at_5", "known_negative_exposure_at_5"),
    ("memory_attributable_gain", "memory_attributable_gain"),
    ("future_query_generalization", "future_query_generalization"),
    ("control_regression_rate", "control_regression"),
    ("generalization_gap", "generalization_gap"),
)


def _find(
    results: list[MetricResult],
    metric_id: str,
    variant_id: str | None = None,
    cohort_id: str | None = None,
) -> Any:
    """The first matching result, narrowed by variant and cohort.

    ``cohort_id`` is not optional in spirit. The same metric is computed for
    every cohort, so a lookup by metric and variant alone returns whichever
    cohort happens to come first in list order -- which is the anchor today and
    is one reordering away from silently becoming the control. The headline
    numbers name the cohort they are about.
    """

    for result in results:
        if result.metric_id != metric_id:
            continue
        if variant_id is not None and result.scope.variant_id != variant_id:
            continue
        if cohort_id is not None and result.scope.cohort_id != cohort_id:
            continue
        return result
    return None


def health_vector(
    results: list[MetricResult], *, primary_variant: str, cohort_id: str | None = None
) -> dict[str, Any]:
    """The headline: named measurements, each with its status and denominator.

    A metric that could not be computed appears with ``value: null`` and its
    status rather than being dropped, because a vector that silently loses a
    dimension reads as a vector where that dimension was fine.
    """

    out: dict[str, Any] = {}
    for metric_id, label in HEALTH_VECTOR:
        result = (
            _find(results, metric_id, primary_variant, cohort_id)
            or _find(results, metric_id, primary_variant)
            or _find(results, metric_id)
        )
        if result is None:
            out[label] = {
                "metric_id": metric_id,
                "value": None,
                "status": MetricStatus.NOT_APPLICABLE.value,
            }
            continue
        out[label] = {
            # The metric's own id travels with the entry, because the label is
            # a *display* name and the two deliberately differ (the vector says
            # `known_positive_retrieval_at_5`, the metric is
            # `known_positive_recall_at_5`). Without it a reader wanting the
            # formula behind a tile has to re-derive the mapping, which means
            # duplicating it in every client -- and a duplicated mapping is one
            # that goes stale in exactly one place.
            "metric_id": result.metric_id,
            "value": result.value,
            "status": result.status,
            "numerator": result.numerator,
            "denominator": result.denominator,
            "classification": result.classification,
        }
    return out


def end_user_explanation(
    results: list[MetricResult],
    gates: list[GateResult],
    *,
    baseline_variant: str,
    treatment_variant: str,
    cohort_id: str | None = None,
) -> str:
    """The paragraph a person reads, built from the numbers it quotes.

    Every figure in the sentence comes from a :class:`MetricResult` in the same
    report, and the coverage clause and the limitation are not optional. The
    specification requires both, and the reason is visible in the example it
    gives: the interesting half of "88.9% versus 73.3%" is "of the 45 queries
    that had evidence at all".
    """

    coverage = _find(results, "query_evidence_coverage", None, cohort_id) or _find(
        results, "query_evidence_coverage"
    )
    recall = _find(results, "known_positive_recall_at_5", treatment_variant, cohort_id) or _find(
        results, "known_positive_recall_at_5", treatment_variant
    )
    base_recall = _find(
        results, "known_positive_recall_at_5", baseline_variant, cohort_id
    ) or _find(results, "known_positive_recall_at_5", baseline_variant)
    gain = _find(results, "memory_attributable_gain", treatment_variant, cohort_id) or _find(
        results, "memory_attributable_gain", treatment_variant
    )
    # Control regression is always about the *control* cohort, never the
    # headline one -- that is the whole point of it being a separate cohort.
    control = _find(results, "control_regression_rate", treatment_variant)
    failed = [gate for gate in gates if not gate.passed]

    parts: list[str] = []
    if recall is not None and recall.value is not None and base_recall is not None:
        if base_recall.value is not None:
            parts.append(
                f"Known-useful content reached the top five for {recall.value:.1%} of evidenced "
                f"queries under {treatment_variant}, against {base_recall.value:.1%} under "
                f"{baseline_variant} "
                f"({int(recall.numerator or 0)} and {int(base_recall.numerator or 0)} of "
                f"{int(recall.denominator or 0)} queries respectively)."
            )
        else:
            parts.append(
                f"Known-useful content reached the top five for {recall.value:.1%} of "
                f"{int(recall.denominator or 0)} evidenced queries."
            )
    if gain is not None and gain.value is not None:
        operands = gain.operands or {}
        parts.append(
            f"The memory system moved the first known-good result by {gain.value:+.3f} reciprocal "
            f"rank on average across {int(gain.denominator or 0)} paired queries "
            f"({operands.get('improved_queries', 0)} improved, "
            f"{operands.get('regressed_queries', 0)} regressed)."
        )
    if control is not None and control.value is not None:
        parts.append(
            "No control query regressed."
            if control.value == 0
            else f"{control.value:.1%} of control queries regressed, which is unintended."
        )
    if coverage is not None and coverage.value is not None:
        parts.append(
            f"Only {int(coverage.numerator or 0)} of {int(coverage.denominator or 0)} eligible "
            f"queries had positive or negative outcome evidence, so this demonstrates a change "
            f"for the evidenced subset rather than exhaustive corpus accuracy."
        )
    else:
        parts.append(
            "No query in this cohort carried outcome evidence, so nothing here demonstrates "
            "retrieval utility -- only structural and operational behaviour was measured."
        )
    if failed:
        parts.append(
            "Hard gates failed: "
            + "; ".join(f"{gate.gate_id} ({gate.detail})" for gate in failed)
            + ". No promotion is possible while these fail."
        )
    return " ".join(parts)


def agent_explanation(
    results: list[MetricResult],
    gates: list[GateResult],
    *,
    snapshot_id: str,
    baseline_variant: str,
    treatment_variant: str,
    sufficiency: dict[str, Any],
    allowed_actions: list[str],
) -> dict[str, Any]:
    """The structured result an agent may act on.

    ``allowed_actions`` is present because an agent handed a report with no
    stated affordances will infer some. Naming them -- and naming them as a
    function of the gates -- is what keeps "the evaluation says memory helps"
    from becoming "so I promoted it".
    """

    deltas = {
        result.metric_id: {
            "value": result.value,
            "denominator": result.denominator,
            "status": result.status,
            "baseline_variant": (result.operands or {}).get("baseline_variant"),
        }
        for result in results
        if result.metric_id
        in {
            "memory_attributable_gain",
            "learned_query_gain",
            "future_query_generalization",
            "generalization_gap",
            "control_regression_rate",
        }
        and result.scope.variant_id == treatment_variant
    }
    attribution = [
        {
            "variant_id": result.scope.variant_id,
            "metric_id": result.metric_id,
            "value": result.value,
            "denominator": result.denominator,
        }
        for result in results
        if result.metric_id == "steering_lift"
    ]
    return {
        "snapshot_id": snapshot_id,
        "baseline_variant": baseline_variant,
        "treatment_variant": treatment_variant,
        "status": "pass" if all(gate.passed for gate in gates) else "fail",
        "evidence_sufficiency": sufficiency,
        "metric_deltas": deltas,
        "attribution": attribution,
        "gates": [gate.as_dict() for gate in gates],
        "limitations": [
            result.does_not_support
            for result in results
            if result.does_not_support and result.scope.variant_id == treatment_variant
        ][:10],
        "allowed_next_actions": allowed_actions,
    }


def developer_explanation(
    results: list[MetricResult],
    per_query: list[MetricResult],
    *,
    snapshot_diff: list[str],
    cohorts: dict[str, Any],
    replay_failures: dict[str, dict[str, str]],
    runtime: dict[str, Any],
    versions: dict[str, Any],
) -> dict[str, Any]:
    """Everything needed to reproduce or dispute a number.

    Worst regressions are surfaced at the top level rather than left inside the
    metric that computed them, because "which queries did this make worse" is
    the first question anyone asks of a positive mean and the last thing a
    nested payload makes easy to find.
    """

    worst: list[dict[str, Any]] = []
    for result in results:
        for item in (result.operands or {}).get("worst_regressions", []) or []:
            worst.append({"metric_id": result.metric_id, **item})
    worst.sort(key=lambda item: item.get("delta", 0.0))
    return {
        "snapshot_diff": snapshot_diff,
        "cohorts": cohorts,
        "metrics": [result.as_dict() for result in results],
        "per_query": [result.as_dict() for result in per_query],
        "worst_regressions": worst[:20],
        "excluded_queries": replay_failures,
        "runtime": runtime,
        "versions": versions,
    }


def composite(results: list[MetricResult], weights: dict[str, float]) -> dict[str, Any]:
    """A weighted geometric mean, only when explicitly configured.

    Off by default and hedged on purpose. Three refusals are built in: a
    missing metric is *excluded* rather than substituted with zero or one; a
    non-positive component makes the product undefined rather than zero; and
    the result is never labelled accuracy. Weights are renormalized over the
    components that were actually available, and the report says which those
    were -- a composite whose weights sum to 0.6 of what was intended is a
    different number from the one the configuration describes.
    """

    if not weights:
        return {"enabled": False, "reason": "no weights configured"}
    included: dict[str, float] = {}
    excluded: dict[str, str] = {}
    for metric_id, weight in weights.items():
        result = _find(results, metric_id)
        if result is None or result.value is None:
            excluded[metric_id] = "not computed"
            continue
        if result.status in {
            MetricStatus.INSUFFICIENT_EVIDENCE.value,
            MetricStatus.NOT_APPLICABLE.value,
        }:
            excluded[metric_id] = result.status
            continue
        if result.value <= 0:
            excluded[metric_id] = "non-positive value: a geometric mean is undefined"
            continue
        included[metric_id] = weight
    if not included:
        return {"enabled": True, "value": None, "included": {}, "excluded": excluded}
    total = sum(included.values())
    value = 1.0
    for metric_id, weight in included.items():
        result = _find(results, metric_id)
        value *= float(result.value) ** (weight / total)
    return {
        "enabled": True,
        "value": round(value, 6),
        "included": {k: round(v / total, 6) for k, v in included.items()},
        "excluded": excluded,
        "label": "weighted geometric mean of the included components",
        "not": "factual accuracy",
    }


def classification_breakdown(results: list[MetricResult]) -> dict[str, list[str]]:
    """Which metrics made which kind of claim.

    Printed in every report so a reader who skims can still tell a
    corpus-relative diagnostic from a demonstrated outcome -- which is the
    distinction the whole specification is organized around.
    """

    out: dict[str, list[str]] = {kind.value: [] for kind in Classification}
    for result in results:
        out.setdefault(result.classification, []).append(result.metric_id)
    return {kind: sorted(set(ids)) for kind, ids in out.items() if ids}
