"""Sizing an evaluation plane: how long a batch runs and what it leaves behind.

Two questions an operator has to answer before provisioning anything, and they
have different shapes:

* **How long does a batch take?** `queries x variants x cohorts` replays, each
  one real search through the real hybrid path. Linear, and the only term worth
  tuning — which is why the honest way to scale evaluation is fewer queries or
  a longer interval rather than more workers.
* **How much volume does it need?** Two answers, deliberately kept apart. The
  *peak* is the replay checkpoints held while a run is in flight and deleted
  when it completes; the *steady state* is metric rows and reports, which
  accumulate. Reporting one number for both would either overstate the steady
  state or understate the peak, and an operator sizing a PVC needs both.

The performance assertions here are **budgets, not benchmarks**. They are loose
enough not to cry wolf on a shared CI runner and tight enough to catch the
shape changing — an O(N^2) creeping into the replay loop is what they exist to
notice, and this repository has shipped exactly that bug once before.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import pheasant.evaluation as evaluation
from pheasant.capacity import (
    BYTES_PER_EVALUATION_METRIC,
    BYTES_PER_EVALUATION_PROOF,
    BYTES_PER_REPLAY_CHECKPOINT_QUERY,
    SECONDS_PER_QUERY_VARIANT,
    project_evaluation,
)
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent
from tests.test_evaluation_batch import _artifact, _engine, _write_config

# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


def test_a_batch_is_linear_in_queries_variants_and_cohorts() -> None:
    """The shape, asserted. If this ever stops being linear, a projection built
    on it is describing a curve as though it were a line — the exact mistake
    `SECONDS_PER_1K_FILES` was quoting before the FTS5 fix."""

    base = project_evaluation(100, variants=6, cohorts=6)
    assert base.replays == 100 * 6 * 6
    assert project_evaluation(200, variants=6, cohorts=6).replays == 2 * base.replays
    assert project_evaluation(100, variants=12, cohorts=6).replays == 2 * base.replays
    assert project_evaluation(100, variants=6, cohorts=12).replays == 2 * base.replays
    assert base.run_seconds == pytest.approx(base.replays * SECONDS_PER_QUERY_VARIANT)


def test_peak_and_steady_state_are_reported_apart() -> None:
    """A PVC has to hold the peak; it fills at the steady rate. One number for
    both would answer neither question."""

    projection = project_evaluation(200, runs_per_day=1.0)
    assert (
        projection.peak_checkpoint_bytes == projection.replays * BYTES_PER_REPLAY_CHECKPOINT_QUERY
    )
    assert projection.state_bytes_per_run > 0
    assert projection.state_bytes_per_year > projection.state_bytes_per_run
    payload = projection.as_dict()
    assert payload["peak_checkpoint_mb"] > 0
    assert payload["state_mb_per_run"] > 0
    assert payload["state_gb_per_year"] > 0


def test_storage_respects_the_per_query_row_cap() -> None:
    """A model that ignored its own configuration knob would overstate a large
    cohort by the ratio between its size and the cap — which is exactly the
    case where the number matters."""

    capped = project_evaluation(5000, max_stored_per_query_results=200)
    uncapped = project_evaluation(5000, max_stored_per_query_results=5000)
    assert capped.state_bytes_per_run < uncapped.state_bytes_per_run / 10
    # Below the cap, the two agree.
    assert (
        project_evaluation(50, max_stored_per_query_results=200).state_bytes_per_run
        == project_evaluation(50, max_stored_per_query_results=5000).state_bytes_per_run
    )


def test_running_more_often_costs_proportionally_more_volume() -> None:
    once = project_evaluation(200, runs_per_day=1.0)
    hourly = project_evaluation(200, runs_per_day=24.0)
    assert hourly.state_bytes_per_year > 20 * once.state_bytes_per_year


def test_a_run_that_would_be_truncated_says_so_before_it_is() -> None:
    """The warning an operator needs *before* provisioning, not after watching
    a report come back with half its cohorts missing."""

    small = project_evaluation(100)
    assert not any("truncated" in warning for warning in small.warnings)
    huge = project_evaluation(20_000)
    assert any("truncated" in warning for warning in huge.warnings)
    assert huge.run_seconds > 900


def test_embeddings_are_recorded_as_an_unknown_rather_than_guessed() -> None:
    """A network embedder's throughput is the provider's rate limit, not
    pheasant's, and a multiplier derived from the stub would describe nothing."""

    from pheasant.capacity import EVALUATION_EMBEDDING_SLOWDOWN

    assert EVALUATION_EMBEDDING_SLOWDOWN is None
    projection = project_evaluation(100, embeddings_enabled=True)
    assert any("rate limit" in warning for warning in projection.warnings)
    # The stated seconds are a floor, not a corrected estimate.
    assert projection.run_seconds == pytest.approx(100 * 6 * 6 * SECONDS_PER_QUERY_VARIANT)


def test_the_projection_serializes_for_scan_and_the_api() -> None:
    payload = project_evaluation(200).as_dict()
    for key in (
        "queries_per_cohort",
        "variants",
        "cohorts",
        "replays_per_run",
        "projected_run_minutes",
        "peak_checkpoint_mb",
        "state_mb_per_run",
        "state_gb_per_year",
        "recommended_memory",
        "warnings",
    ):
        assert key in payload, key
    assert payload["recommended_memory"].endswith("Gi")


# --------------------------------------------------------------------------
# The model against the real thing
# --------------------------------------------------------------------------


def _bulk_seed(engine: Any, queries: int) -> None:
    """Enough recorded traffic to make a cohort of the requested size."""

    target = _artifact(engine, "invoice")
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id=f"s{index % 8}",
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:00.{index:06d}Z",
                status="ok",
                duration_ms=8.0,
                query_text=f"question number {index} about invoice retry",
                result_paths=["invoice.md"],
                result_ids=[target],
                result_count=1,
                top_score=0.8,
            )
            for index in range(queries)
        ],
    )
    for index in range(0, queries, 4):
        evaluation.record_evidence(
            engine.state,
            engine.config,
            query=f"question number {index} about invoice retry",
            target_id=target,
            event_type="selected",
            interaction_id=f"seed-{index}",
        )


def test_the_scan_projection_describes_this_regions_configuration(tmp_path: Path) -> None:
    """Read off the live config, not the model's defaults: a deployment that
    trimmed the matrix should see the smaller number it earned."""

    engine = _engine(tmp_path)
    try:
        full = engine.scan_source("docs")["evaluation_projection"]
        assert full is not None
        assert full["variants"] == 6
        assert full["cohorts"] == 6

        engine.config.evaluation.variants.alias_only = False
        engine.config.evaluation.variants.preference_only = False
        engine.config.evaluation.cohorts.rolling = False
        trimmed = engine.scan_source("docs")["evaluation_projection"]
        assert trimmed["variants"] == 4
        assert trimmed["cohorts"] == 5
        assert trimmed["replays_per_run"] < full["replays_per_run"]
    finally:
        engine.close()


def test_no_evaluation_projection_when_evaluation_is_off(tmp_path: Path) -> None:
    """An operator who has not enabled it does not need a volume estimate."""

    engine = _engine(tmp_path)
    try:
        engine.config.evaluation.enabled = False
        assert engine.scan_source("docs")["evaluation_projection"] is None
    finally:
        engine.close()


@pytest.mark.parametrize("queries", [40])
def test_a_real_batch_lands_inside_its_projected_budget(tmp_path: Path, queries: int) -> None:
    """The model against the machine.

    A budget rather than a benchmark: 40x is enormously loose, because a shared
    CI runner is noisy and a threshold that cries wolf is a threshold people
    disable. What it catches is the *shape* changing — a replay loop that went
    quadratic would blow through this at any cohort size worth running.
    """

    config, path = _write_config(tmp_path)
    config.evaluation.cohorts.anchor_minimum_queries = 5
    config.evaluation.cohorts.maximum_queries_per_cohort = queries
    from pheasant.api.app import create_app

    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    _bulk_seed(engine, queries)
    try:
        started = time.monotonic()
        outcome = evaluation.run(engine)
        elapsed = time.monotonic() - started
        assert outcome.status == "completed"

        replays = outcome.report["explanations"]["developer"]["runtime"]["queries_replayed"]
        projected = project_evaluation(
            queries,
            variants=6,
            cohorts=6,
        ).run_seconds
        assert replays > 0
        assert elapsed < max(30.0, projected * 40), (
            f"a {replays}-query batch took {elapsed:.1f}s against a projected {projected:.1f}s; "
            "the replay loop's cost per query has changed shape"
        )
    finally:
        engine.close()


def test_stored_bytes_per_row_are_the_right_order_of_magnitude(tmp_path: Path) -> None:
    """The coefficients, checked against real rows rather than trusted.

    Order of magnitude only — the exact size moves with SQLite's page layout
    and with how much a given corpus puts in an operand bag. Being 3x out is
    what makes a volume estimate useless, and that is what this catches.
    """

    config, path = _write_config(tmp_path)
    config.evaluation.cohorts.anchor_minimum_queries = 5
    from pheasant.api.app import create_app

    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    _bulk_seed(engine, 24)
    try:
        evaluation.run(engine)
        metrics = engine.state.rows(
            "SELECT payload_json FROM evaluation_metrics WHERE query_id IS NOT NULL LIMIT 200"
        )
        assert metrics
        mean_metric = sum(len(str(row["payload_json"])) for row in metrics) / len(metrics)
        assert BYTES_PER_EVALUATION_METRIC / 4 < mean_metric < BYTES_PER_EVALUATION_METRIC * 4, (
            f"a stored metric row averages {mean_metric:.0f} bytes against a modelled "
            f"{BYTES_PER_EVALUATION_METRIC}; the capacity projection is out by more than 4x"
        )

        proofs = engine.state.rows("SELECT * FROM evaluation_proofs LIMIT 200")
        assert proofs
        mean_proof = sum(sum(len(str(v)) for v in dict(row).values()) for row in proofs) / len(
            proofs
        )
        assert mean_proof < BYTES_PER_EVALUATION_PROOF, (
            f"a proof row's payload averages {mean_proof:.0f} bytes, over the modelled "
            f"{BYTES_PER_EVALUATION_PROOF} which is meant to include index overhead"
        )
    finally:
        engine.close()


def test_checkpoints_do_not_outlive_the_run_that_needed_them(tmp_path: Path) -> None:
    """The peak is transient by design. If it were not, the checkpoint table
    would grow by a copy of every result list, forever, for no reader."""

    config, path = _write_config(tmp_path)
    config.evaluation.cohorts.anchor_minimum_queries = 5
    from pheasant.api.app import create_app

    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    _bulk_seed(engine, 16)
    try:
        evaluation.run(engine)
        assert engine.state.rows("SELECT COUNT(*) AS c FROM evaluation_replays")[0]["c"] == 0
    finally:
        engine.close()


# --------------------------------------------------------------------------
# The benchmark that produces the coefficients
# --------------------------------------------------------------------------


def test_the_benchmark_measures_a_real_batch_and_checks_the_model() -> None:
    """A model whose numbers nobody checks against a machine is a model that
    quietly stops describing anything.

    Small on purpose — this runs in the ordinary suite, and the CI job runs it
    at a size worth publishing. What is under test is that the comparison
    exists and is honest, not the constants themselves.
    """

    from pheasant.evaluation.benchmark import run_benchmark

    report = run_benchmark(queries=8, files=20, seed=7)
    assert report["status"] == "completed"

    measured = report["measured"]
    assert measured["replays"] > 0
    assert measured["run_seconds"] > 0
    assert measured["ms_per_replay"] > 0
    # The peak is sampled *during* the run. Measuring afterwards would report
    # zero, because the checkpoints are cleared on completion — and would call
    # a transient cost free.
    assert measured["peak_checkpoint_bytes"] > 0
    assert measured["metric_rows"] > 0

    projected = report["projected"]
    assert projected["replays_per_run"] > 0
    assert projected["peak_checkpoint_bytes"] > 0

    # The ladder is the table an operator sizing a volume actually reads, so it
    # has to be ordered and monotonic in the thing it claims to vary.
    ladder = report["ladder"]
    sizes = [row["queries_per_cohort"] for row in ladder]
    assert sizes == sorted(sizes)
    assert [row["replays_per_run"] for row in ladder] == sorted(
        row["replays_per_run"] for row in ladder
    )


def test_the_modelled_peak_is_not_below_what_a_real_run_holds() -> None:
    """The one coefficient whose error has a direction that matters.

    Understating the peak is what makes a volume fill mid-run. The first value
    shipped here was 3x under, found by running this benchmark rather than by
    reading the code.
    """

    from pheasant.evaluation.benchmark import run_benchmark

    report = run_benchmark(queries=8, files=20, seed=11)
    measured = report["measured"]["peak_checkpoint_bytes"]
    modelled = project_evaluation(
        report["measured"]["queries"],
        variants=report["measured"]["variants"],
        cohorts=report["measured"]["cohorts"],
    ).peak_checkpoint_bytes
    assert modelled >= measured * 0.75, (
        f"the model projects {modelled} bytes of checkpoints where a real run held "
        f"{measured}; a volume sized from this would fill mid-run"
    )
