"""Acceptance tests for context search with provenance."""

from __future__ import annotations

from tests.conftest import item_text, result_items, run_sync, search_context


def test_search_context_returns_sync_engine_file_with_provenance(loaded_config: object, sync_engine: object) -> None:
    """A sync-engine query should return relevant chunks/files and source provenance."""

    run_sync(sync_engine, source_name="syncsage-repo", mode="full")

    search_result = search_context("sync engine", loaded_config=loaded_config, engine=sync_engine)
    items = result_items(search_result)

    assert items, "Expected at least one search result for 'sync engine'."
    flattened = "\n".join(item_text(item).lower() for item in items)
    assert "sync" in flattened and "engine" in flattened
    assert "sync_engine.py" in flattened or "sync-engine.md" in flattened or "readme.md" in flattened
    assert "provenance" in flattened or "source" in flattened or "path" in flattened
