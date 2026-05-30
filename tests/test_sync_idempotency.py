"""Acceptance tests for idempotent source synchronization."""

from __future__ import annotations

from tests.conftest import run_sync, sync_result_counts


def test_full_sync_is_idempotent_for_sample_repository(sync_engine: object) -> None:
    """Running a full sync twice should not duplicate graph/search artifacts."""

    first_result = run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    first_counts = sync_result_counts(first_result, sync_engine)

    second_result = run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    second_counts = sync_result_counts(second_result, sync_engine)

    assert second_counts == first_counts
    assert all(value > 0 for value in second_counts.values())


def test_incremental_sync_repairs_missing_graph_snapshot(
    loaded_config: object,
    sync_engine: object,
) -> None:
    """Unchanged-file skips should not preserve a graph reduced to only the root."""

    run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    sync_engine.graph_builder.remove_source_content("syncsage-repo")
    assert sync_engine.graph_builder.graph.number_of_nodes() == 1

    result = run_sync(sync_engine, source_name="syncsage-repo", mode="incremental")
    graph = sync_engine.graph_builder.graph
    source_node = f"source:{loaded_config.knowledge_base_id}:syncsage-repo"

    assert result.indexed_artifacts > 0
    assert graph.has_node(source_node)
    assert graph.number_of_nodes() > 1
