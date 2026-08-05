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
