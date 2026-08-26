"""Acceptance tests for idempotent source synchronization."""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.persistence.graph_store import GraphStore
from pheasant.sync.engine import SyncEngine
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


def test_incremental_noop_does_not_materialize_the_persisted_graph(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(config_path)
    source = cfg.sources[0]
    writer = SyncEngine(cfg)
    try:
        first = writer.sync_source(source.name, "full")
        expected_counts = (first.graph_nodes, first.graph_edges)
    finally:
        writer.close()

    def unexpected_load(_self: GraphStore, _kb_id: str):
        raise AssertionError("unchanged incremental sync loaded the whole graph")

    monkeypatch.setattr(GraphStore, "load", unexpected_load)
    reader = SyncEngine(cfg, defer_persisted_graph_load=True)
    try:
        reader.ensure_node_index()
        result = reader.sync_source(source.name, "incremental")
    finally:
        reader.close()

    assert result.indexed_artifacts == 0
    assert (result.graph_nodes, result.graph_edges) == expected_counts
