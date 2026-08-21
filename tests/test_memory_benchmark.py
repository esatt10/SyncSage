"""Product Framework Step 33.4 — memory-recall benchmark (closes Phase 33).

The harness runs the real write→index→search path; the thresholds below are
the regression gate for the two retrieval fixes it flushed out (FTS5
implicit-AND on natural-language questions, inverted bm25→relevance
mapping). Full-size numbers are recorded in the Flock repo's
``docs/RESULTS.md`` §9d; reproduce with ``python -m pheasant.memory.benchmark``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.memory.benchmark import (
    MemoryBenchSpec,
    generate_cases,
    run_memory_recall_benchmark,
)

CI_SPEC = MemoryBenchSpec(n_facts=12, n_distractors=40, n_updates=4, n_abstain=4)

#: Every test here runs with the default `memory.supersede_retention_days: 0`
#: (no explicit `memory:` block in `_tools`'s config) — deliberately.
#: Enabling retention (Phase 2) keeps a corrected record's near-duplicate
#: text indexed alongside its correction, and that measurably costs
#: `update_accuracy`: observed to swing 0.75-1.0 run to run on this exact
#: seed with `supersede_retention_days=7`, from hybrid RRF fusion giving the
#: still-indexed (but query-time-filtered) old record and its correction two
#: close competitors for one query instead of one. `stale_leak_rate` stayed
#: 0.0 throughout — the old record never leaked into a result set, so this
#: is a ranking-competition cost, not a correctness bug. See
#: `docs/memory-system.md` §4 and `tests/test_memory_retention.py`.


def _tools(tmp_path: Path) -> PheasantTools:
    (tmp_path / "memory").mkdir(exist_ok=True)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: membench
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return PheasantTools(load_config(config_path))


def test_case_generation_is_deterministic() -> None:
    assert generate_cases(CI_SPEC) == generate_cases(CI_SPEC)
    assert generate_cases(MemoryBenchSpec(seed=7)) != generate_cases(MemoryBenchSpec(seed=8))


def test_memory_recall_benchmark_meets_thresholds(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    try:
        report = run_memory_recall_benchmark(tools, CI_SPEC)
    finally:
        tools.engine.close()
    assert report.recall_at_k >= 0.9, report.as_dict()
    assert report.update_accuracy >= 0.9, report.as_dict()
    assert report.stale_leak_rate == 0.0, report.as_dict()
    assert report.abstention_accuracy >= 0.9, report.as_dict()


def test_natural_language_question_ranks_the_right_memory_first(tmp_path: Path) -> None:
    """The regression the benchmark caught: an NL question must not zero out
    on FTS5 implicit-AND, and bm25 order must survive the score mapping."""
    tools = _tools(tmp_path)
    try:
        record = tools.memory_write(
            "membench", "The paydb failover runbook lives in the ops vault.", scope="org"
        )["record"]
        for i in range(10):
            tools.memory_write("membench", f"Note {i}: the sprint review logged item.", scope="org")
        tools.sync_source("membench", "agent-memory", "incremental")
        results = tools.search_context(
            "membench", "where is the paydb failover runbook", mode="text", max_results=5
        )["results"]
        assert results, "natural-language question returned nothing"
        assert record["record_id"] in str(results[0].get("relative_path"))
    finally:
        tools.engine.close()


def test_benchmark_requires_a_memory_source(tmp_path: Path) -> None:
    config_path = tmp_path / "s.yaml"
    (tmp_path / "docs").mkdir()
    config_path.write_text(
        f"""pheasant:
  name: nomem
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sources:
  - name: notes
    type: document_folder
    path: docs
""",
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    try:
        with pytest.raises(ValueError, match="memory"):
            run_memory_recall_benchmark(tools, CI_SPEC)
    finally:
        tools.engine.close()
