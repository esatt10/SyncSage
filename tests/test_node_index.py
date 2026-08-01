"""The graph node index is a cache: fast when present, invisible when not.

Graph-mode search used to score every node in the graph. The index exists to
make that O(matches) instead of O(graph) — but the graph stays the only source
of truth, so every test here checks that removing, emptying or breaking the
index changes speed and nothing else.
"""

from __future__ import annotations

from syncsage.config.loader import load_config
from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.search.graph_search import search_graph
from syncsage.search.node_index import NodeIndex
from syncsage.sync.engine import SyncEngine
from tests.conftest import CONFIG_TEMPLATE


def _graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node(
        "file:repo:executor.py",
        id="file:repo:executor.py",
        type="file",
        label="workflow/executor.py",
        relative_path="workflow/executor.py",
        source_id="repo",
    )
    graph.add_node(
        "symbol:repo:dispatch",
        id="symbol:repo:dispatch",
        type="symbol",
        label="dispatch",
        source_id="repo",
    )
    graph.add_node(
        "file:other:readme.md",
        id="file:other:readme.md",
        type="markdown_note",
        label="README.md",
        source_id="other",
    )
    return graph


def _render_config(tmp_path, workspace):
    base = tmp_path / "idx"
    for sub in ("state", "vault", "exports"):
        (base / sub).mkdir(parents=True)
    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace.as_posix(),
        state_path=(base / "state").as_posix(),
        vault_path=(base / "vault").as_posix(),
        exports_path=(base / "exports").as_posix(),
    )
    config_file = base / "syncsage.yaml"
    config_file.write_text(rendered, encoding="utf-8")
    return config_file


def test_indexed_results_are_a_ranked_subset_of_scanning(tmp_path, workspace_copy) -> None:
    """The contract the index actually offers.

    Candidates come from token/prefix matching, where the scan used arbitrary
    substrings — so ``sync`` still finds ``syncsage`` (prefix) but no longer
    finds ``resync`` (infix). That makes indexed results a **subset** of
    scanned ones, never a superset: the index must not invent a hit. Among the
    hits both paths agree on, the ranking must be identical, because ranking
    is still the original scorer's job.
    """

    engine = SyncEngine(load_config(_render_config(tmp_path, workspace_copy)))
    try:
        engine.graph_builder.graph = _graph()
        engine.rebuild_node_index()

        for query in ("executor", "dispatch", "readme", "workflow", "nothing-matches-this"):
            indexed = search_graph(engine.graph_builder.graph, query, node_index=engine.node_index)
            scanned = search_graph(engine.graph_builder.graph, query)
            indexed_ids = [hit["node_id"] for hit in indexed]
            scanned_ids = [hit["node_id"] for hit in scanned]
            assert set(indexed_ids) <= set(scanned_ids), f"index invented a hit for {query!r}"
            # Relative order is preserved for everything they share.
            shared = [node_id for node_id in scanned_ids if node_id in set(indexed_ids)]
            assert indexed_ids == shared, f"index reordered results for {query!r}"
    finally:
        engine.close()


def test_an_empty_or_missing_index_falls_back_to_scanning(tmp_path, workspace_copy) -> None:
    """An index that cannot answer must say so, not answer 'nothing'."""

    engine = SyncEngine(load_config(_render_config(tmp_path, workspace_copy)))
    try:
        engine.graph_builder.graph = _graph()
        # Never built: candidates() returns None, so search scans and finds it.
        assert engine.node_index.candidates(["executor"]) is None
        hits = search_graph(engine.graph_builder.graph, "executor", node_index=engine.node_index)
        assert [hit["node_id"] for hit in hits] == ["file:repo:executor.py"]
    finally:
        engine.close()


def test_index_tracks_writes_and_removals(tmp_path, workspace_copy) -> None:
    """A stale index would silently hide nodes from search."""

    engine = SyncEngine(load_config(_render_config(tmp_path, workspace_copy)))
    try:
        graph = _graph()
        engine.graph_builder.graph = graph
        engine.rebuild_node_index()

        # Add a node, flush the delta, and it becomes findable.
        graph.add_node(
            "symbol:repo:route_message",
            id="symbol:repo:route_message",
            type="symbol",
            label="route_message",
            source_id="repo",
        )
        engine.flush_node_index()
        assert engine.node_index.candidates(["route_message"]) == ["symbol:repo:route_message"]

        # Remove it, flush, and it is gone from the index too.
        graph.remove_nodes_from(["symbol:repo:route_message"])
        engine.flush_node_index()
        assert engine.node_index.candidates(["route_message"]) == []
    finally:
        engine.close()


def test_source_scope_is_honoured(tmp_path, workspace_copy) -> None:
    engine = SyncEngine(load_config(_render_config(tmp_path, workspace_copy)))
    try:
        engine.graph_builder.graph = _graph()
        engine.rebuild_node_index()
        assert engine.node_index.candidates(["readme"], source_name="other") == [
            "file:other:readme.md"
        ]
        assert engine.node_index.candidates(["readme"], source_name="repo") == []
    finally:
        engine.close()


def test_a_real_sync_leaves_the_index_usable(tmp_path, workspace_copy) -> None:
    """After an ordinary sync, graph search should not need to scan."""

    engine = SyncEngine(load_config(_render_config(tmp_path, workspace_copy)))
    try:
        engine.sync_source("syncsage-repo", "full")
        assert engine.node_index.count() > 0, "sync did not populate the node index"
        candidates = engine.node_index.candidates(["sync"])
        assert candidates, "an indexed term returned no candidates"
        for node_id in candidates:
            assert engine.graph_builder.graph.has_node(node_id), "index names a node that is gone"
    finally:
        engine.close()


def test_index_is_disposable(tmp_path, workspace_copy) -> None:
    """Deleting the index must cost speed, never data."""

    engine = SyncEngine(load_config(_render_config(tmp_path, workspace_copy)))
    try:
        engine.sync_source("syncsage-repo", "full")
        with_index = search_graph(engine.graph_builder.graph, "sync", node_index=engine.node_index)
        assert with_index, "indexed search found nothing to begin with"

        with engine.state.conn:
            engine.state.conn.execute("DELETE FROM graph_nodes_fts")
        assert NodeIndex(engine.state).count() == 0

        # No index: search still answers, from the graph alone. It may find
        # *more* than the index did (substring beats prefix), never less.
        scanned = search_graph(engine.graph_builder.graph, "sync", node_index=engine.node_index)
        assert scanned, "search stopped working without its cache"
        assert {h["node_id"] for h in with_index} <= {h["node_id"] for h in scanned}

        # And the cache rebuilds from the graph alone.
        assert engine.rebuild_node_index() == engine.graph_builder.graph.number_of_nodes()
    finally:
        engine.close()
