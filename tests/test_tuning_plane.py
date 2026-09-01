"""The pieces: stage attribution, the parameter space, the strategy, the gates.

`test_tuning_batch.py` pins the whole pass. This pins the reasoning, because
that is where this plane can be wrong in ways that still produce plausible
numbers — an attribution that blames the wrong stage sends the search into a
space that cannot contain the answer, and the search will still find
*something*.
"""

from __future__ import annotations

import pytest

from pheasant.search.ranking import DEFAULT_RANKING
from pheasant.tuning import gates as gate_checks
from pheasant.tuning.contracts import Comparison, Decision, ParameterPoint, Proposal, Trial
from pheasant.tuning.space import ParameterSpace, baseline_values, validate_space
from pheasant.tuning.stages import ACTIONABLE_STAGES, attribute, stage_histogram
from pheasant.tuning.strategy import Budget, halving_schedule, propose, select_survivors


def stages(**overrides):
    """A stage block shaped like the one `search_context(explain=True)` returns."""

    block = {
        "candidates": {"text": ["a", "b", "c"], "vector": [], "graph": []},
        "surviving": {"text": ["a", "b", "c"], "vector": [], "graph": []},
        "filters": {},
        "fusion": {"ranked": ["a", "b", "c"], "scores": {}},
        "returned": ["a", "b"],
        "paths": {"a": "a.md", "b": "b.md", "c": "c.md"},
        "arms_run": ["text"],
        "arms_failed": [],
        "max_results": 2,
    }
    block.update(overrides)
    return block


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


def test_a_returned_target_is_served_with_its_rank() -> None:
    result = attribute(stages(), "a", max_results=2)
    assert result.stage == "served"
    assert result.detail["served_rank"] == 1


def test_a_target_below_the_cut_is_a_fusion_miss_not_a_truncation_one() -> None:
    """Two failures that look identical downstream and have nothing in common.

    Fused 3rd with max_results=2 is a *ranking* problem — the merge put two
    worse results above it. Calling that truncation would point the fix at
    `max_results`, which is the caller's, not the region's.
    """

    result = attribute(stages(), "c", max_results=2)
    assert result.stage == "fusion"
    assert result.detail["fused_rank"] == 3
    assert result.actionable


def test_a_filtered_target_names_the_filter_that_removed_it() -> None:
    """An arm had it and something took it away. Which something matters."""

    block = stages(
        surviving={"text": ["a", "b"], "vector": [], "graph": []},
        filters={"acl": {"text": ["c"]}},
        fusion={"ranked": ["a", "b"], "scores": {}},
    )
    result = attribute(block, "c", max_results=2)
    assert result.stage == "filters"
    assert result.detail["removed_by"] == ["acl"]
    assert "acl" in result.reason


def test_an_unindexed_target_is_never_blamed_on_retrieval() -> None:
    """The most important refusal in the module.

    No ranking parameter can move a document that is not in the index, and a
    diagnosis that blamed the lexical arm for one would send an operator to
    tune weights for a week.
    """

    block = stages(candidates={"text": ["a"], "vector": [], "graph": []})
    result = attribute(block, "z", max_results=2, indexed=lambda _id: False)
    assert result.stage == "absent_from_corpus"
    assert not result.actionable
    assert result.stage not in ACTIONABLE_STAGES


def test_absence_is_not_inferred_without_a_corpus_check() -> None:
    """ "No arm returned it" and "it is not indexed" are different claims.

    Without a lookup they are indistinguishable, and this plane does not
    upgrade a guess to a finding.
    """

    block = stages(candidates={"text": ["a"], "vector": [], "graph": []})
    result = attribute(block, "z", max_results=2)
    assert result.stage == "candidates_missing"
    assert "not checked" in result.reason


def test_an_indexed_target_no_arm_returned_is_an_arm_miss() -> None:
    block = stages(candidates={"text": ["a"], "vector": [], "graph": []})
    result = attribute(block, "z", max_results=2, indexed=lambda _id: True)
    assert result.stage == "lexical_candidates"
    assert result.actionable


def test_one_arm_missing_it_is_not_a_miss_when_another_arm_had_it() -> None:
    """Hybrid retrieval is allowed to have a weak arm.

    Blaming the vector arm for a miss the text arm covered is how a diagnosis
    ends up recommending a re-embed that changes nothing — and the per-arm fact
    is still *reported*, because a consistently empty arm is worth knowing.
    """

    block = stages(
        candidates={"text": ["a", "b"], "vector": ["q"], "graph": []},
        surviving={"text": ["a", "b"], "vector": ["q"], "graph": []},
        arms_run=["text", "vector"],
    )
    result = attribute(block, "a", max_results=2)
    assert result.stage == "served"
    assert result.detail["arms_missing_target"] == ["vector"]


def test_a_failed_arm_is_reported_separately_from_an_empty_one() -> None:
    """ "The vector arm is down" and "it has nothing here" call for opposite fixes."""

    block = stages(arms_failed=["vector"], arms_run=["text", "vector"])
    result = attribute(block, "a", max_results=2)
    assert result.detail["arms_failed"] == ["vector"]


# --------------------------------------------------------------------------
# The histogram
# --------------------------------------------------------------------------


def test_the_histogram_separates_actionable_misses_from_the_rest() -> None:
    """ "43% of misses are in fusion" and "43% were never indexed" are opposite
    instructions in the same sentence shape."""

    block_hit = stages()
    block_absent = stages(candidates={"text": ["a"], "vector": [], "graph": []})
    attributions = [
        attribute(block_hit, "a", max_results=2),
        attribute(block_hit, "c", max_results=2),
        *[
            attribute(block_absent, f"z{i}", max_results=2, indexed=lambda _id: False)
            for i in range(3)
        ],
    ]
    histogram = stage_histogram(attributions)
    assert histogram["evaluated"] == 5
    assert histogram["served"] == 1
    assert histogram["misses"] == 4
    assert histogram["actionable_misses"] == 1
    assert histogram["actionable_share"] == pytest.approx(0.25)
    assert histogram["dominant_stage"] == "absent_from_corpus"


def test_a_histogram_with_no_misses_reports_no_share_rather_than_zero() -> None:
    """Null means "nothing to attribute"; 0.0 would mean "nothing is tunable"."""

    histogram = stage_histogram([attribute(stages(), "a", max_results=2)])
    assert histogram["misses"] == 0
    assert histogram["actionable_share"] is None


# --------------------------------------------------------------------------
# The space and the strategy
# --------------------------------------------------------------------------


def test_the_shipped_space_is_internally_consistent() -> None:
    assert validate_space(ParameterSpace()) == []


def test_a_space_whose_stages_disagree_with_ranking_is_refused() -> None:
    """A silent failure otherwise: the search runs, reports numbers, and is
    looking in the wrong place."""

    from dataclasses import replace

    space = ParameterSpace()
    wrong = replace(space.by_name("rrf_k"), stage="lexical_candidates")
    broken = ParameterSpace(parameters=(wrong,))
    problems = validate_space(broken)
    assert problems
    assert "rrf_k" in problems[0]


def test_a_fusion_parameter_must_be_cheap_and_a_candidate_one_must_not_be() -> None:
    """The budget's whole premise. A mislabelled cost class would either waste
    the expensive budget or report re-fused numbers for a parameter that
    changes what the arms retrieve."""

    space = ParameterSpace()
    for parameter in space.parameters:
        if parameter.stage == "fusion":
            assert parameter.cost_class == "refusion"
        else:
            assert parameter.cost_class == "requery"


def test_the_strategy_only_proposes_parameters_for_blamed_stages() -> None:
    space = ParameterSpace()
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "fusion", "count": 10}],
        "counts": {"fusion": 10},
        "misses": 10,
        "actionable_share": 1.0,
    }
    proposals = propose(space, baseline, histogram)
    assert proposals
    assert {p.motivating_stage for p in proposals} == {"fusion"}
    assert {p.cost_class for p in proposals} == {"refusion"}


def test_the_strategy_declines_when_nothing_it_controls_is_to_blame() -> None:
    """A tuning pass that reports "do not tune" has done its job."""

    space = ParameterSpace()
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "absent_from_corpus", "count": 9}],
        "counts": {"absent_from_corpus": 9},
        "misses": 10,
        "actionable_share": 0.1,
    }
    assert propose(space, baseline, histogram) == []


def test_a_pinned_parameter_is_never_proposed() -> None:
    """An operator who has measured their own title weight should not have a
    later sweep quietly re-litigate it."""

    space = ParameterSpace(pinned=frozenset({"rrf_k"}))
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "fusion", "count": 10}],
        "counts": {"fusion": 10},
        "misses": 10,
        "actionable_share": 1.0,
    }
    names = {name for p in propose(space, baseline, histogram) for name in p.point.delta}
    assert "rrf_k" not in names
    assert space.by_name("rrf_k") is None


def test_every_proposal_carries_a_rationale_naming_its_stage() -> None:
    """Traceability, at the point it is cheapest to enforce."""

    space = ParameterSpace()
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "fusion", "count": 7}],
        "counts": {"fusion": 7},
        "misses": 10,
        "actionable_share": 1.0,
    }
    for proposal in propose(space, baseline, histogram):
        assert "fusion" in proposal.rationale
        assert proposal.point.delta
        assert proposal.point.describe_delta() != "baseline"


def test_proposals_move_one_coordinate_at_a_time() -> None:
    """A joint search over twelve parameters has enough combinations to find
    an apparent winner in pure noise."""

    space = ParameterSpace()
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "fusion", "count": 7}, {"stage": "lexical_candidates", "count": 3}],
        "counts": {"fusion": 7, "lexical_candidates": 3},
        "misses": 10,
        "actionable_share": 1.0,
    }
    for proposal in propose(space, baseline, histogram):
        assert len(proposal.point.delta) == 1, proposal.point.delta


def test_a_point_is_addressed_by_its_values() -> None:
    """The same point proposed twice by two strategies is one point and one
    trial, which is what makes a descent that revisits a coordinate free."""

    a = ParameterPoint.of({"rrf_k": 30.0, "text_arm_weight": 1.0})
    b = ParameterPoint.of({"text_arm_weight": 1.0, "rrf_k": 30.0})
    assert a.point_id == b.point_id


def test_survivors_break_ties_deterministically() -> None:
    """Two replicas that enumerated proposals in different orders must carry
    the same survivors forward, or a resumed batch computes different numbers."""

    scored = [("pt-b", 0.5), ("pt-a", 0.5), ("pt-c", 0.4)]
    assert select_survivors(scored, 2) == ["pt-a", "pt-b"]
    assert select_survivors(list(reversed(scored)), 2) == ["pt-a", "pt-b"]


def test_halving_degrades_to_one_round_when_there_is_nothing_to_halve() -> None:
    assert halving_schedule(list(range(40)), 2) == [(40, 2)]
    assert halving_schedule(list(range(3)), 12) == [(3, 12)]
    schedule = halving_schedule(list(range(40)), 12)
    assert schedule[-1][0] == 40
    assert schedule[-1][1] < schedule[0][1]


def test_the_budget_separates_the_two_cost_classes() -> None:
    """One number would be spent entirely on whichever class the enumeration
    reached first."""

    space = ParameterSpace()
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "fusion", "count": 7}, {"stage": "lexical_candidates", "count": 5}],
        "counts": {"fusion": 7, "lexical_candidates": 5},
        "misses": 12,
        "actionable_share": 1.0,
    }
    proposals = propose(
        space, baseline, histogram, budget=Budget(refusion_trials=3, requery_trials=1)
    )
    assert sum(1 for p in proposals if p.cost_class == "refusion") == 3
    assert sum(1 for p in proposals if p.cost_class == "requery") == 1


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def comparison(delta: float, paired: int = 50) -> Comparison:
    return Comparison(
        metric="known_positive_reciprocal_rank",
        baseline_trial_id="t0",
        treatment_trial_id="t1",
        baseline_value=0.5,
        treatment_value=0.5 + delta,
        delta=delta,
        paired_queries=paired,
    )


def test_an_improvement_with_no_holdout_is_not_promotable() -> None:
    """The single most important gate.

    A point that improved the queries it was *selected on* has demonstrated
    selection, not improvement. On the search cohort every winner looks like a
    winner, which is exactly why this cannot be checked there.
    """

    results = gate_checks.evaluate(
        search_comparison=comparison(0.2),
        holdout_comparison=None,
        control_comparison=comparison(0.0),
        baseline_histogram={"counts": {}, "evaluated": 50},
        winning_histogram={"counts": {}, "evaluated": 50},
        parameters={"rrf_k": 30.0},
    )
    assert "holdout_confirms" in gate_checks.blocking_failures(results)


def test_a_control_regression_blocks_promotion() -> None:
    """Retrieval is a fixed number of slots; almost any change that helps one
    class of query hurts another."""

    results = gate_checks.evaluate(
        search_comparison=comparison(0.2),
        holdout_comparison=comparison(0.05),
        control_comparison=comparison(-0.4),
        baseline_histogram={"counts": {}, "evaluated": 50},
        winning_histogram={"counts": {}, "evaluated": 50},
        parameters={"rrf_k": 30.0},
    )
    assert "control_does_not_regress" in gate_checks.blocking_failures(results)


def test_emptying_a_stage_blocks_promotion() -> None:
    """A point can lift the headline while moving an arm's exclusive hits into
    `candidates_missing`. Sometimes that is right; it must never be silent."""

    results = gate_checks.evaluate(
        search_comparison=comparison(0.2),
        holdout_comparison=comparison(0.05),
        control_comparison=comparison(0.0),
        baseline_histogram={"counts": {"vector_candidates": 0}, "evaluated": 50},
        winning_histogram={"counts": {"vector_candidates": 30}, "evaluated": 50},
        parameters={"vector_arm_weight": 0.0},
    )
    assert "no_stage_collapse" in gate_checks.blocking_failures(results)


def test_a_thin_denominator_blocks_promotion() -> None:
    """Six paired queries can produce any delta you like."""

    results = gate_checks.evaluate(
        search_comparison=comparison(0.4, paired=6),
        holdout_comparison=comparison(0.4, paired=6),
        control_comparison=comparison(0.0, paired=6),
        baseline_histogram={"counts": {}, "evaluated": 6},
        winning_histogram={"counts": {}, "evaluated": 6},
        parameters={"rrf_k": 30.0},
    )
    assert "sufficient_evidence" in gate_checks.blocking_failures(results)


def test_a_clean_result_passes_every_gate() -> None:
    results = gate_checks.evaluate(
        search_comparison=comparison(0.2),
        holdout_comparison=comparison(0.05),
        control_comparison=comparison(0.0),
        baseline_histogram={"counts": {"fusion": 20}, "evaluated": 50},
        winning_histogram={"counts": {"fusion": 5}, "evaluated": 50},
        parameters={"rrf_k": 30.0},
    )
    assert gate_checks.blocking_failures(results) == []


def test_an_empty_gate_list_is_a_failure_not_a_pass() -> None:
    """`all([])` is True, and the evaluation plane shipped a version where a
    skipped run therefore reported that its gates passed — straight into a CLI
    exit status."""

    assert not Decision(
        decision_id="d", experiment_id="e", outcome="promote", reason="", gates=()
    ).gates_passed
    assert Decision(
        decision_id="d",
        experiment_id="e",
        outcome="promote",
        reason="",
        gates=({"gate_id": "g", "passed": True},),
    ).gates_passed


def test_gates_are_always_produced() -> None:
    results = gate_checks.evaluate(
        search_comparison=None,
        holdout_comparison=None,
        control_comparison=None,
        baseline_histogram={},
        winning_histogram={},
        parameters={},
    )
    assert results


# --------------------------------------------------------------------------
# Trial validation
# --------------------------------------------------------------------------


def test_a_trial_without_a_denominator_cannot_be_stored() -> None:
    """Validated before the statement, not inside the transaction: a batch of
    trials shares one, and a rolled-back transaction cannot drop the one bad
    row and keep the rest."""

    point = ParameterPoint.of({"rrf_k": 30.0}, parent=ParameterPoint.of({"rrf_k": 60.0}))
    trial = Trial(
        trial_id="t",
        experiment_id="e",
        proposal=Proposal(point=point, motivating_stage="fusion", rationale="because"),
        cohort_id="c",
        cohort_name="rolling",
        metrics={"known_positive_reciprocal_rank": 0.5},
        evaluated_queries=0,
    )
    assert "no denominator: zero queries evaluated" in trial.validate()

    trial.evaluated_queries = 10
    assert trial.validate() == []


def test_a_trial_that_names_no_stage_cannot_be_stored() -> None:
    point = ParameterPoint.of({"rrf_k": 30.0}, parent=ParameterPoint.of({"rrf_k": 60.0}))
    trial = Trial(
        trial_id="t",
        experiment_id="e",
        proposal=Proposal(point=point, motivating_stage="", rationale="because"),
        cohort_id="c",
        cohort_name="rolling",
        metrics={"known_positive_reciprocal_rank": 0.5},
        evaluated_queries=10,
    )
    assert "proposal names no motivating stage" in trial.validate()
