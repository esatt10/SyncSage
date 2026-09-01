"""The objective, the glossary, and the lineage a promotion leaves behind.

Three things that are easy to build wrong in ways nothing fails on: an
objective that silently assumes one product decision, an explanation catalog
that drifts from the code it explains, and a promotion history that cannot
answer "what were we serving before".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.tuning import glossary
from pheasant.tuning import objective as objective_module
from pheasant.tuning import store as tuning_store
from pheasant.tuning.contracts import TuningBundle
from tests.test_tuning_batch import _engine

# --------------------------------------------------------------------------
# The objective
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(objective_module.BUILTIN))
def test_every_objective_states_what_it_trades_away(name: str) -> None:
    """An objective without a stated trade is a preference presented as an optimum.

    Mechanically enforced because it is exactly the field that gets dropped:
    the summary is the fun part to write and the trade is the part that stops
    somebody optimizing for a caller they do not have.
    """

    objective = objective_module.BUILTIN[name]
    assert objective.trades_away.strip(), name
    assert objective.summary.strip(), name
    assert objective.weights, name
    assert abs(sum(objective.normalized().values()) - 1.0) < 1e-9


def test_a_composite_scores_none_rather_than_zero_when_a_component_is_missing() -> None:
    """A point that could not be measured is not a point that measured badly.

    Scoring a missing metric as zero would rank the unmeasurable below the
    merely poor and call the ordering a result.
    """

    balanced = objective_module.BUILTIN["balanced"]
    assert balanced.score({"known_positive_reciprocal_rank": 0.5}) is None
    assert balanced.score(
        {"known_positive_reciprocal_rank": 0.5, "known_positive_recall_at_10": 0.9}
    ) == pytest.approx(0.7)


def test_a_composite_publishes_its_arithmetic() -> None:
    """What makes a combined score arguable rather than merely readable."""

    balanced = objective_module.BUILTIN["balanced"]
    substituted = balanced.substituted(
        {"known_positive_reciprocal_rank": 0.5, "known_positive_recall_at_10": 0.9}
    )
    assert "0.5" in substituted and "0.9" in substituted and "0.7" in substituted


def test_custom_weights_win_over_a_name() -> None:
    """A caller who wrote weights has been more specific than one who picked a label."""

    from pheasant.config.schema import TuningObjectiveSettings

    resolved = objective_module.resolve(
        TuningObjectiveSettings(metric="hit_rate", weights={"known_positive_hit_rate": 2.0})
    )
    assert resolved.objective_id == "custom"
    assert resolved.normalized() == {"known_positive_hit_rate": 1.0}


def test_an_unknown_objective_falls_back_rather_than_raising() -> None:
    """A typo should cost the objective somebody meant, not every batch.

    Visible rather than silent: the resolved objective is published on every
    report, so the fallback shows up where a reader will see it.
    """

    from pheasant.config.schema import TuningObjectiveSettings

    resolved = objective_module.resolve(TuningObjectiveSettings(metric="reciprocol_rank"))
    assert resolved.objective_id == objective_module.DEFAULT_OBJECTIVE


def test_the_objective_reaches_the_report(tmp_path: Path) -> None:
    """A report naming a winner without naming its objective is unreadable later."""

    from pheasant.tuning.runner import run_tuning
    from pheasant.tuning.strategy import Budget
    from tests.test_tuning_batch import _seed

    engine = _engine(tmp_path)
    _seed(engine)
    engine.config.tuning.objective.metric = "recall_at_10"
    try:
        outcome = run_tuning(engine, budget=Budget(refusion_trials=4, requery_trials=1))
        assert outcome.status == "completed", outcome.skipped_reason
        published = outcome.report["objective"]
        assert published["objective_id"] == "recall_at_10"
        assert published["trades_away"]
        assert published["baseline_substituted"]
    finally:
        engine.close()


def test_each_mechanism_is_measured_on_its_own(tmp_path: Path) -> None:
    """ "Hybrid is better" is an assumption most regions never test.

    The ablation costs no retrieval — the arms already ran, so isolating one
    is a re-fusion with the others weighted to zero.
    """

    from pheasant.tuning.runner import run_tuning
    from pheasant.tuning.strategy import Budget
    from tests.test_tuning_batch import _seed

    engine = _engine(tmp_path)
    _seed(engine)
    try:
        outcome = run_tuning(engine, budget=Budget(refusion_trials=4, requery_trials=1))
        mechanisms = outcome.mechanisms
        assert "hybrid" in mechanisms
        assert {"text", "vector", "graph"} & set(mechanisms)
        for arm, entry in mechanisms.items():
            assert entry["evaluated_queries"] > 0, arm
            if arm != "hybrid":
                # What the merge is worth over this arm alone. Negative is a
                # legitimate and important finding, not an error.
                assert "hybrid_gain" in entry, arm
        assert outcome.report["mechanisms"] == mechanisms
    finally:
        engine.close()


# --------------------------------------------------------------------------
# The glossary
# --------------------------------------------------------------------------


def test_every_glossary_entry_says_what_it_does_not_mean() -> None:
    """The field that prevents the wrong action, and the one most likely to be skipped."""

    catalog = glossary.catalog()
    for group in ("metrics", "health", "stages", "gates", "parameters"):
        assert catalog[group], group
        for entry in catalog[group]:
            assert entry["means"].strip(), entry["term"]
            assert entry["impact"].strip(), entry["term"]
            assert entry["does_not_mean"].strip(), entry["term"]
            assert entry["direction"] in {"higher", "lower", "neutral"}, entry["term"]


def test_every_pipeline_stage_is_explained() -> None:
    """A stage the UI can render is a stage a reader can be confused by.

    Drift here is silent: a new stage renders with an empty explanation and
    nothing fails, which is how a diagnosis becomes a word nobody can act on.
    """

    from pheasant.tuning.stages import STAGES

    explained = {entry["term"] for entry in glossary.STAGES}
    assert set(STAGES) <= explained, sorted(set(STAGES) - explained)


def test_every_gate_the_plane_emits_is_explained() -> None:
    from pheasant.tuning import gates as gate_checks

    emitted = {
        item["gate_id"]
        for item in gate_checks.evaluate(
            search_comparison=None,
            holdout_comparison=None,
            control_comparison=None,
            baseline_histogram={},
            winning_histogram={},
            parameters={},
        )
    }
    explained = {entry["term"] for entry in glossary.GATES}
    assert emitted <= explained, sorted(emitted - explained)


def test_every_tunable_parameter_is_explained() -> None:
    """The catalog names its own gaps rather than hiding them."""

    catalog = glossary.catalog()
    # `prefer_bonus` and `prior_floor` are bounded but not searched by the
    # shipped space, so they are legitimately absent — and *named*, which is
    # what makes that a decision rather than an oversight.
    assert catalog["parameters_without_explanation"] == ["prefer_bonus", "prior_floor"]
    for entry in catalog["parameters"]:
        assert entry["means"].strip(), entry["term"]


def test_a_glossary_lookup_finds_entries_across_every_group() -> None:
    assert glossary.lookup("truncation_rate")["kind"] == "health"
    assert glossary.lookup("fusion")["kind"] == "stage"
    assert glossary.lookup("holdout_confirms")["kind"] == "gate"
    assert glossary.lookup("rrf_k")["kind"] == "parameter"
    assert glossary.lookup("nonsense") is None


# --------------------------------------------------------------------------
# Base, lineage and reversibility
# --------------------------------------------------------------------------


def _bundle(params: dict[str, float]) -> TuningBundle:
    return TuningBundle(
        bundle_id=TuningBundle.identity(params),
        kb_id="kb",
        experiment_id="exp-lineage",
        decision_id="dec-lineage",
        snapshot_id="snap",
        parameters=params,
    )


def test_base_and_overlay_are_reported_separately(tmp_path: Path) -> None:
    """ "What is it ranking with" and "what would a rollback give me" differ."""

    import pheasant.tuning as tuning

    engine = _engine(tmp_path)
    try:
        before = tuning.active_parameters(engine.state, "kb", engine.config)
        assert before["provenance"] == "config"
        assert before["overlay"]["values"] == {}
        assert before["changes"] == []

        bundle = _bundle({"rrf_k": 25.0})
        tuning_store.save_bundle(engine.state, bundle)
        tuning_store.apply_bundle(engine.state, "kb", bundle.bundle_id, applied_by="test")

        after = tuning.active_parameters(engine.state, "kb", engine.config)
        assert after["provenance"] == "bundle"
        assert after["base"]["values"]["rrf_k"] == 60.0
        assert after["values"]["rrf_k"] == 25.0
        assert after["changes"] == [
            {"parameter": "rrf_k", "stage": "fusion", "base": 60.0, "active": 25.0}
        ]
    finally:
        engine.close()


def test_lineage_records_what_each_promotion_replaced(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        first = _bundle({"rrf_k": 25.0})
        second = _bundle({"rrf_k": 90.0})
        for bundle in (first, second):
            tuning_store.save_bundle(engine.state, bundle)
            tuning_store.apply_bundle(engine.state, "kb", bundle.bundle_id, applied_by="test")

        history = tuning_store.lineage(engine.state, "kb")
        assert [entry["bundle_id"] for entry in history] == [
            second.bundle_id,
            first.bundle_id,
        ]
        assert history[0]["active"] is True
        assert history[1]["active"] is False
        # The newest replaced the first; the first replaced the base, which is
        # recorded as an empty map rather than as the defaults.
        assert history[0]["replaced"] == {"rrf_k": 25.0}
        assert history[1]["replaced"] == {}
    finally:
        engine.close()


def test_rollback_can_target_an_earlier_bundle(tmp_path: Path) -> None:
    """The last apply made things worse and the one before it was fine.

    Forcing that through "revert to base, then re-apply" loses the fact that
    it was a rollback; naming a target records it as one.
    """

    import pheasant.tuning as tuning

    engine = _engine(tmp_path)
    try:
        first = _bundle({"rrf_k": 25.0})
        second = _bundle({"rrf_k": 90.0})
        for bundle in (first, second):
            tuning_store.save_bundle(engine.state, bundle)
            tuning_store.apply_bundle(engine.state, "kb", bundle.bundle_id, applied_by="test")

        tuning_store.revert_bundle(engine.state, "kb", applied_by="test", to=first.bundle_id)
        active = tuning.active_parameters(engine.state, "kb", engine.config)
        assert active["values"]["rrf_k"] == 25.0
        assert active["overlay"]["bundle_id"] == first.bundle_id
        assert "rollback" in active["overlay"]["applied_by"]

        # And back to base from there.
        tuning_store.revert_bundle(engine.state, "kb", applied_by="test")
        assert tuning_store.active_overlay(engine.state, "kb") is None
    finally:
        engine.close()


def test_only_one_bundle_is_ever_active_through_a_rollback(tmp_path: Path) -> None:
    """Two active overlays would make two replicas rank differently."""

    engine = _engine(tmp_path)
    try:
        first = _bundle({"rrf_k": 25.0})
        second = _bundle({"rrf_k": 90.0})
        for bundle in (first, second):
            tuning_store.save_bundle(engine.state, bundle)
            tuning_store.apply_bundle(engine.state, "kb", bundle.bundle_id, applied_by="test")
        tuning_store.revert_bundle(engine.state, "kb", applied_by="test", to=first.bundle_id)

        active = [b for b in tuning_store.list_bundles(engine.state, "kb") if b["active"]]
        assert len(active) == 1
        assert active[0]["bundle_id"] == first.bundle_id
    finally:
        engine.close()
