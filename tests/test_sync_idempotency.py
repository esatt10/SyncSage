"""Acceptance tests for idempotent source synchronization."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import make_vector_engine, run_sync, sync_result_counts


def test_full_sync_is_idempotent_for_sample_repository(sync_engine: object) -> None:
    """Running a full sync twice should not duplicate graph/search artifacts."""

    first_result = run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    first_counts = sync_result_counts(first_result, sync_engine)

    second_result = run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    second_counts = sync_result_counts(second_result, sync_engine)

    assert second_counts == first_counts
    assert all(value > 0 for value in second_counts.values())


def test_resync_of_unchanged_content_performs_zero_embedder_calls(tmp_path: Path) -> None:
    """Synapse 21.4: embed-on-sync is keyed on text_hash via content-addressed
    chunk ids, so re-syncing unchanged content never re-embeds — in full or
    incremental mode."""

    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    embedder = engine.vectors.embedder
    store = engine.vectors.store
    assert embedder.texts_embedded > 0
    baseline_calls = embedder.calls
    baseline_count = store.count()

    run_sync(engine, source_name="notes", mode="full")
    assert embedder.calls == baseline_calls
    assert store.count() == baseline_count

    run_sync(engine, source_name="notes", mode="incremental")
    assert embedder.calls == baseline_calls
    assert store.count() == baseline_count


# ---------------------------------------------------------------------------
# Deferred enrichment (agent-speed memory compaction, Phase 0)
# ---------------------------------------------------------------------------
#
# `memory_write`'s default `sync=True` calls `sync_source(..., enrich="deferred")`
# so a single interactive write never pays the whole-graph walk
# (`add_similarity_edges` + `add_cross_source_edges` + `_bridge_memory`,
# `sync/engine.py:_finalize_index_state`). CLAUDE.md rule 4: any sync change
# owes idempotency cases here.


def _about_edge_count(tools) -> int:
    graph = tools.engine.graph_builder.graph
    n = 0
    with graph.reading():
        for _endpoints, edge_map in graph.iter_edges():
            n += sum(1 for data in edge_map.values() if data.get("type") == "about")
    return n


def _deferred_enrichment_tools(tmp_path: Path):
    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "runbook.md").write_text(
        "# Runbook\n\nThe Kestrel Gateway is operated by the Platform Team.\n",
        encoding="utf-8",
    )
    (tmp_path / "memory").mkdir()
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: deferred-enrich
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: corpus
    type: document_folder
    path: corpus
    include: ["**/*.md"]
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("deferred-enrich", "corpus", "full")
    return tools


def test_a_deferred_memory_write_skips_the_whole_graph_walk(tmp_path: Path) -> None:
    """The write itself must not draw the `about` edge that only
    `_bridge_memory` (the whole-graph walk) draws, and must mark the source
    enrichment-dirty so the next pass knows there is work to do."""

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        dirty_scope = tools.engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        assert tools.engine.state.get_fingerprint(dirty_scope) is None

        tools.memory_write(
            "deferred-enrich",
            "See the runbook for how the Kestrel Gateway is operated.",
            scope="org",
        )

        assert _about_edge_count(tools) == 0, "a deferred write ran the whole-graph walk"
        assert tools.engine.state.get_fingerprint(dirty_scope) is not None
    finally:
        tools.engine.close()


def test_the_next_beat_enriches_once_and_a_second_beat_does_nothing(tmp_path: Path) -> None:
    """`enrich="deferred"` defers, it does not skip: the next `enrich="now"`
    pass (an ordinary incremental sync — the shape the scheduler beat takes)
    must produce the same graph a non-deferred write would have, and clear
    the dirty flag so a second beat with nothing new runs none of the three
    enrichment steps again."""

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        dirty_scope = tools.engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        tools.memory_write(
            "deferred-enrich",
            "See the runbook for how the Kestrel Gateway is operated.",
            scope="org",
        )
        assert _about_edge_count(tools) == 0

        tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        assert tools.engine.state.get_fingerprint(dirty_scope) is None
        first_pass_edges = _about_edge_count(tools)
        assert first_pass_edges > 0, "the deferred enrichment never ran"

        calls = {"similarity": 0, "cross_source": 0, "bridge": 0}
        builder = tools.engine.graph_builder
        original_similarity = builder.add_similarity_edges
        original_cross_source = builder.add_cross_source_edges
        original_bridge = tools.engine._bridge_memory

        def spy_similarity(*args, **kwargs):
            calls["similarity"] += 1
            return original_similarity(*args, **kwargs)

        def spy_cross_source(*args, **kwargs):
            calls["cross_source"] += 1
            return original_cross_source(*args, **kwargs)

        def spy_bridge(*args, **kwargs):
            calls["bridge"] += 1
            return original_bridge(*args, **kwargs)

        builder.add_similarity_edges = spy_similarity
        builder.add_cross_source_edges = spy_cross_source
        tools.engine._bridge_memory = spy_bridge
        try:
            tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        finally:
            builder.add_similarity_edges = original_similarity
            builder.add_cross_source_edges = original_cross_source
            tools.engine._bridge_memory = original_bridge

        assert calls == {"similarity": 0, "cross_source": 0, "bridge": 0}, calls
        assert _about_edge_count(tools) == first_pass_edges
    finally:
        tools.engine.close()


def test_a_no_op_memory_write_never_marks_the_source_dirty(tmp_path: Path) -> None:
    """An exact-duplicate write (`created=False`) indexes nothing new, so it
    must not mark the source enrichment-dirty — there is nothing for the
    next pass to pick up."""

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        dirty_scope = tools.engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        tools.memory_write("deferred-enrich", "A note nobody references.", scope="org")
        tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        assert tools.engine.state.get_fingerprint(dirty_scope) is None

        result = tools.memory_write("deferred-enrich", "A note nobody references.", scope="org")
        assert not result["created"]
        assert tools.engine.state.get_fingerprint(dirty_scope) is None
    finally:
        tools.engine.close()
