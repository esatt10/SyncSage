"""Stopping and starting a container must not re-index anything.

The state directory holds the artifacts, chunks, vectors and graph. A restart
over unchanged content with an unchanged config is bookkeeping, not work — and
the things that *do* invalidate stored state each get repaired the cheap way
rather than by re-reading the world.
"""

from __future__ import annotations

import json
from pathlib import Path

from pheasant.config.loader import load_config
from pheasant.sync.engine import SyncEngine
from pheasant.sync.fingerprint import (
    EMBEDDING_SCOPE,
    SOURCE_SCOPE,
    embedding_change,
    embedding_fingerprint,
    source_fingerprint,
)
from tests.conftest import CONFIG_TEMPLATE


def _render_config(tmp_path: Path, name: str, workspace: Path) -> Path:
    base = tmp_path / name
    for sub in ("state", "vault", "exports"):
        (base / sub).mkdir(parents=True)
    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace.as_posix(),
        state_path=(base / "state").as_posix(),
        vault_path=(base / "vault").as_posix(),
        exports_path=(base / "exports").as_posix(),
    )
    config_file = base / "pheasant.yaml"
    config_file.write_text(rendered, encoding="utf-8")
    return config_file


def _engine(config_file: Path) -> SyncEngine:
    return SyncEngine(load_config(config_file))


def _read_graph(path: Path) -> dict:
    """The stored graph is zstd-compressed node-link JSON."""

    import zstandard as zstd

    return json.loads(zstd.ZstdDecompressor().decompress(path.read_bytes()))


def test_restart_reindexes_nothing_and_keeps_the_graph(tmp_path, workspace_copy) -> None:
    config_file = _render_config(tmp_path, "restart", workspace_copy)

    first = _engine(config_file)
    try:
        # Index everything the way a first run does.
        indexed = sum(result.indexed_artifacts for result in first.startup())
        nodes = first.graph_builder.graph.number_of_nodes()
        assert indexed > 0
    finally:
        first.close()

    # "Restart": a brand-new engine over the same state directory.
    second = _engine(config_file)
    try:
        assert second.graph_builder.graph.number_of_nodes() == nodes, "graph did not survive"
        results = second.startup()
        assert results, "startup did not sync the on_startup sources"
        assert sum(r.indexed_artifacts for r in results) == 0, "a restart re-indexed content"
        assert sum(r.skipped_artifacts for r in results) == indexed
        assert second.graph_builder.graph.number_of_nodes() == nodes
    finally:
        second.close()


def test_changing_what_a_source_indexes_forces_a_full_pass(tmp_path, workspace_copy) -> None:
    """Globs and chunking decide what text exists, so changing them redoes it."""

    config_file = _render_config(tmp_path, "fp", workspace_copy)
    engine = _engine(config_file)
    try:
        engine.sync_source("pheasant-repo", "full")
        source = engine._source("pheasant-repo")
        stored = engine.state.get_fingerprint(SOURCE_SCOPE.format(name="pheasant-repo"))
        assert stored == source_fingerprint(engine.config.effective_source(source))

        # An unrelated edit must NOT invalidate anything.
        engine.config.assistant.model = "some-other-model"
        assert engine.sync_source("pheasant-repo", "incremental").indexed_artifacts == 0

        # A chunking change must.
        source.chunking.max_chars = int(source.chunking.max_chars) // 2
        result = engine.sync_source("pheasant-repo", "incremental")
        assert result.indexed_artifacts > 0, "re-chunking did not re-index"
        assert engine.state.get_fingerprint(SOURCE_SCOPE.format(name="pheasant-repo")) != stored
    finally:
        engine.close()


def test_graph_and_manifest_are_checkpointed_mid_sync(tmp_path, workspace_copy) -> None:
    """An interrupted sync must not throw away everything it had built.

    Both halves matter: the graph is the structure, and the manifest is what
    the next incremental pass reads to decide a file is unchanged. Persisting
    one without the other still costs a full re-read on resume.
    """

    config_file = _render_config(tmp_path, "ckpt", workspace_copy)
    engine = _engine(config_file)
    try:
        engine.config.storage.graph_checkpoint_seconds = 0  # checkpoint only on demand
        engine.sync_source("pheasant-repo", "full")
        graph_path = engine.graph_store.graph_path(engine.config.knowledge_base_id)
        saved = _read_graph(graph_path)
        assert len(saved["nodes"]) == engine.graph_builder.graph.number_of_nodes()

        # Work happening after the last save, then a shutdown.
        engine.graph_builder.upsert_node("late:node", "concept", "late", {})
        engine._graph_dirty = True
        manifest = {"source_id": "pheasant-repo", "artifacts": {"late.md": {"sha256": "a" * 64}}}
        assert engine.checkpoint_graph("pheasant-repo", manifest) is True

        reloaded = _read_graph(graph_path)
        assert any(node.get("id") == "late:node" for node in reloaded["nodes"])
        assert "late.md" in engine.manifests.load("pheasant-repo").get("artifacts", {})
        # Nothing dirty: a second checkpoint writes nothing.
        assert engine.checkpoint_graph("pheasant-repo", manifest) is False
    finally:
        engine.close()


def test_repair_heals_a_graph_gap_without_re_reading_everything(tmp_path, workspace_copy) -> None:
    """A lost graph is rebuilt by repair — not by a full re-index."""

    config_file = _render_config(tmp_path, "gap", workspace_copy)
    engine = _engine(config_file)
    graph_path = engine.graph_store.graph_path(engine.config.knowledge_base_id)
    try:
        indexed = engine.sync_source("pheasant-repo", "full").indexed_artifacts
        assert indexed > 0
    finally:
        engine.close()

    # Lose the graph the way an interrupted first sync does: SQLite still has
    # every artifact, the node-link file does not.
    graph_path.unlink()

    recovered = _engine(config_file)
    try:
        assert recovered.graph_builder.graph.number_of_nodes() <= 1
        assert recovered._graph_gap("pheasant-repo") is True
        recovered.startup()
        # Every artifact SQLite knows about is back on the graph...
        assert recovered._graph_gap("pheasant-repo") is False
        rows = recovered.state.rows("SELECT id FROM artifacts WHERE source_id='pheasant-repo'")
        for row in rows:
            assert recovered.graph_builder.graph.has_node(str(row["id"]))
    finally:
        recovered.close()

    # ...and the next restart is free again.
    settled = _engine(config_file)
    try:
        assert sum(r.indexed_artifacts for r in settled.startup()) == 0
    finally:
        settled.close()


def test_embedding_transitions_are_classified() -> None:
    assert embedding_change("abc", "abc") == "none"
    assert embedding_change(None, "abc") == "introduced"
    assert embedding_change("disabled", "abc") == "introduced"
    assert embedding_change("abc", "xyz") == "invalidated"
    assert embedding_change("abc", "disabled") == "disabled"


def test_embedding_dimension_change_re_embeds_without_re_reading(tmp_path, workspace_copy) -> None:
    """A new vector space is rebuilt from stored chunks, not from the files."""

    config_file = _render_config(tmp_path, "embed", workspace_copy)
    engine = _engine(config_file)
    try:
        engine.config.search.embeddings.enabled = True
        engine.config.search.embeddings.provider = "stub"
        engine.config.search.embeddings.dimensions = 8
        engine.config.search.vector_store.provider = "numpy"
        from pheasant.search.vector_store import vector_indexer_from_config

        engine.vectors = vector_indexer_from_config(engine.config)
        assert engine.vectors is not None

        engine.sync_source("pheasant-repo", "full")
        first = engine.reconcile_embeddings()
        assert first["change"] == "introduced"
        assert engine.state.get_fingerprint(EMBEDDING_SCOPE) == embedding_fingerprint(engine.config)

        # Unchanged config: a restart embeds nothing at all.
        assert engine.reconcile_embeddings() == {"change": "none", "embedded": 0, "dropped": False}

        # Dimension change: old vectors are dropped and everything is
        # re-embedded. Proving it never touches the sources: rename the
        # workspace out from under it first. Re-embedding reads SQLite only,
        # so a vanished source directory is irrelevant to it.
        workspace_copy.rename(workspace_copy.with_name("moved-away"))
        engine.config.search.embeddings.dimensions = 16
        engine.vectors = vector_indexer_from_config(engine.config)
        report = engine.reconcile_embeddings()
        assert report["change"] == "invalidated"
        assert report["dropped"] is True
        assert report["embedded"] > 0
    finally:
        engine.close()
