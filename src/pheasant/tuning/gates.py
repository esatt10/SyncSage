"""What must hold before a parameter set is allowed to change the region.

Gates are not metrics, and the distinction is the same one the evaluation
plane draws: a gate is evaluated *before* aggregation and *outside* the score,
so a good number cannot offset a broken invariant. A parameter set that lifts
the primary metric by 0.08 while breaking ACL filtering is not a trade-off to
weigh. It is a rejection.

The five gates below exist because there are exactly five ways this plane could
fool itself, and each gate closes one.

``holdout_confirms``
    The winning point was *selected* on the search cohort. A point that
    improved the queries it was chosen on has demonstrated selection, not
    improvement. It has to improve a cohort the search never saw. This is the
    single most important gate here, and it is the one a naive implementation
    omits, because on the search cohort every winner looks like a winner.

``control_does_not_regress``
    Retrieval is a fixed number of slots. Almost any parameter change that
    helps one class of query hurts another, and a cohort assembled from
    evidenced queries is systematically unlike the traffic that produced no
    evidence at all. The control cohort is the queries the experiment is *not*
    trying to improve, and it must not get worse.

``no_stage_collapse``
    A point can lift the headline metric while emptying an arm -- setting a
    weight to zero moves every one of that arm's exclusive hits into
    ``candidates_missing``. Sometimes that is right (a stale vector index) and
    the gate does not forbid it; it forbids it happening *unnoticed*, by
    failing when a stage's miss count grows beyond a threshold the decision
    then has to state.

``sufficient_evidence``
    A denominator gate. Six paired queries can produce any delta you like.

``parameters_within_bounds``
    Cheap, and it catches the case where a bug in the search produces a point
    the region would clamp on read -- which would make the applied
    configuration silently different from the measured one.

**An empty gate list is a failure, not a pass.** ``all([])`` is ``True``, and
the evaluation plane shipped a version where a skipped run therefore reported
that its gates passed -- straight into a CLI exit status. :func:`evaluate`
always returns at least one gate, and :meth:`Decision.gates_passed` requires a
non-empty list.
"""

from __future__ import annotations

from typing import Any

from pheasant import decision
from pheasant.search.ranking import BOUNDS, PARAMETER_STAGES
from pheasant.tuning.contracts import Comparison

#: A holdout delta below this counts as "not confirmed". Not zero: a delta of
#: +0.0001 on a holdout is indistinguishable from noise, and treating it as
#: confirmation would make the gate a formality that passes whenever the sign
#: happens to be right.
HOLDOUT_MIN_DELTA = 0.005

#: How far the control cohort may fall. Small and non-zero: exactly zero would
#: fail on floating-point noise and on a single query moving between two
#: results of equal relevance.
CONTROL_MAX_REGRESSION = 0.01

#: Paired queries required before a comparison is allowed to decide anything.
MIN_PAIRED_QUERIES = 20

#: How much a single stage's miss count may grow, as a share of the evaluated
#: queries, before the change counts as a collapse.
MAX_STAGE_GROWTH_SHARE = 0.15


def gate(
    gate_id: str,
    passed: bool,
    *,
    summary: str,
    observed: Any = None,
    threshold: Any = None,
    blocking: bool = True,
) -> dict[str, Any]:
    """One gate result, in the shape everything downstream reads.

    ``observed`` and ``threshold`` are carried separately from ``summary`` so
    a UI can render the comparison and a reader can argue with the number
    rather than with a sentence about it.

    Built through :class:`pheasant.decision.Gate` and returned as its dict, so
    the wire shape that decision records and the UI already read is unchanged
    while the vocabulary has one definition.
    """

    return decision.Gate(
        gate_id=gate_id,
        passed=bool(passed),
        summary=summary,
        observed=observed,
        threshold=threshold,
        blocking=blocking,
    ).as_dict()


def evaluate(
    *,
    search_comparison: Comparison | None,
    holdout_comparison: Comparison | None,
    control_comparison: Comparison | None,
    baseline_histogram: dict[str, Any],
    winning_histogram: dict[str, Any],
    parameters: dict[str, float],
    min_paired: int = MIN_PAIRED_QUERIES,
) -> list[dict[str, Any]]:
    """Every gate, evaluated. Never returns an empty list."""

    gates: list[dict[str, Any]] = []

    # --- evidence ---------------------------------------------------------
    paired = search_comparison.paired_queries if search_comparison else 0
    gates.append(
        gate(
            "sufficient_evidence",
            paired >= min_paired,
            summary=(
                f"{paired} paired queries on the search cohort; "
                f"{min_paired} required before a delta may decide anything"
            ),
            observed=paired,
            threshold=min_paired,
        )
    )

    # --- holdout ----------------------------------------------------------
    if holdout_comparison is None:
        gates.append(
            gate(
                "holdout_confirms",
                False,
                summary=(
                    "no holdout cohort was available, so the improvement could not be "
                    "separated from selection on the queries the search chose it with"
                ),
                observed=None,
                threshold=HOLDOUT_MIN_DELTA,
            )
        )
    else:
        gates.append(
            gate(
                "holdout_confirms",
                holdout_comparison.delta >= HOLDOUT_MIN_DELTA
                and holdout_comparison.paired_queries >= min_paired,
                summary=(
                    f"holdout delta {holdout_comparison.delta:+.4f} over "
                    f"{holdout_comparison.paired_queries} paired queries "
                    f"({holdout_comparison.substituted})"
                ),
                observed=holdout_comparison.delta,
                threshold=HOLDOUT_MIN_DELTA,
            )
        )

    # --- control ----------------------------------------------------------
    if control_comparison is None:
        # Not fatal on its own: a region may legitimately have no control
        # cohort yet. Reported as a non-blocking failure so the decision says
        # what it could not check rather than implying it checked and passed.
        gates.append(
            gate(
                "control_does_not_regress",
                False,
                summary="no control cohort was available; regression elsewhere was not checked",
                blocking=False,
            )
        )
    else:
        gates.append(
            gate(
                "control_does_not_regress",
                control_comparison.delta >= -CONTROL_MAX_REGRESSION,
                summary=(
                    f"control delta {control_comparison.delta:+.4f} over "
                    f"{control_comparison.paired_queries} paired queries; "
                    f"a fall beyond {CONTROL_MAX_REGRESSION} blocks promotion"
                ),
                observed=control_comparison.delta,
                threshold=-CONTROL_MAX_REGRESSION,
            )
        )

    # --- stage collapse ---------------------------------------------------
    evaluated = max(1, int(winning_histogram.get("evaluated") or 0))
    before = baseline_histogram.get("counts") or {}
    after = winning_histogram.get("counts") or {}
    grown = {
        stage: (int(after.get(stage, 0)) - int(before.get(stage, 0)))
        for stage in set(before) | set(after)
        if stage != "served"
    }
    worst_stage, worst_growth = max(grown.items(), key=lambda pair: pair[1], default=("", 0))
    share = worst_growth / evaluated
    gates.append(
        gate(
            "no_stage_collapse",
            share <= MAX_STAGE_GROWTH_SHARE,
            summary=(
                f"the largest stage regression is {worst_stage or 'none'} "
                f"(+{worst_growth} misses, {share:.1%} of {evaluated} queries); "
                f"above {MAX_STAGE_GROWTH_SHARE:.0%} the change is trading one "
                "failure mode for another rather than fixing anything"
            ),
            observed=share,
            threshold=MAX_STAGE_GROWTH_SHARE,
        )
    )

    # --- bounds -----------------------------------------------------------
    outside = {
        name: value
        for name, value in parameters.items()
        if name in BOUNDS and not BOUNDS[name][0] <= float(value) <= BOUNDS[name][1]
    }
    unknown = sorted(set(parameters) - set(PARAMETER_STAGES))
    gates.append(
        gate(
            "parameters_within_bounds",
            not outside and not unknown,
            summary=(
                "every parameter is a known ranking parameter inside its bounds"
                if not outside and not unknown
                else f"out of bounds: {outside or {}}; unknown: {unknown}"
            ),
            observed={"out_of_bounds": outside, "unknown": unknown},
        )
    )
    return gates


def blocking_failures(gates: list[dict[str, Any]]) -> list[str]:
    """Gate ids that failed and are allowed to stop a promotion."""

    return decision.blocking_failures(gates)
