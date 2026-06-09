"""Tests for graph-aware search across nodes, relationships and attributes."""

from __future__ import annotations

from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.search.graph_search import search_graph
from syncsage.search.hybrid import HybridSearch


def _sample_graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node(
        "file:repo:sync_engine.py",
        id="file:repo:sync_engine.py",
        type="file",
        label="syncsage/sync_engine.py",
        relative_path="syncsage/sync_engine.py",
        source_id="repo",
    )
    graph.add_node(
        "symbol:repo:SyncEngine",
        id="symbol:repo:SyncEngine",
        type="symbol",
        label="SyncEngine",
        name="SyncEngine",
        symbol_type="class",
        source_id="repo",
    )
    graph.add_node(
        "concept:repo:debounce",
        id="concept:repo:debounce",
        type="concept",
        label="debounce window",
        source_id="repo",
    )
    graph.add_node(
        "external_reference:repo:httpx",
        id="external_reference:repo:httpx",
        type="external_reference",
        label="httpx",
        reference="httpx",
        reference_type="python_import",
        source_id="repo",
    )
    graph.add_edge(
        "file:repo:sync_engine.py",
        "external_reference:repo:httpx",
        type="imports",
        source_id="repo",
    )
    graph.add_edge(
        "file:repo:sync_engine.py",
        "symbol:repo:SyncEngine",
        type="mentions",
        source_id="repo",
    )
    return graph


def test_graph_search_matches_node_label() -> None:
    results = search_graph(_sample_graph(), "SyncEngine", max_results=5)
    assert results, "expected a node hit for SyncEngine"
    top = results[0]
    assert top["kind"] == "node"
    assert top["node_id"] == "symbol:repo:SyncEngine"
    assert top["type"] == "symbol"


def test_graph_search_matches_attribute_value() -> None:
    # 'httpx' lives only in an attribute (reference), not the chunk text.
    results = search_graph(_sample_graph(), "httpx", max_results=5)
    node_ids = {r["node_id"] for r in results if r["kind"] == "node"}
    assert "external_reference:repo:httpx" in node_ids


def test_graph_search_matches_relationship_type() -> None:
    results = search_graph(_sample_graph(), "imports", max_results=10)
    rels = [r for r in results if r["kind"] == "relationship"]
    assert any(r["edge_type"] == "imports" for r in rels)


def test_graph_search_respects_source_scope() -> None:
    results = search_graph(_sample_graph(), "SyncEngine", max_results=5, source_name="other")
    assert results == []


def test_graph_search_honours_max_results() -> None:
    results = search_graph(_sample_graph(), "engine", max_results=2)
    assert len(results) <= 2


class _EmptyStore:
    def search(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
        return []


def test_hybrid_graph_mode_uses_graph_when_text_empty() -> None:
    hybrid = HybridSearch(_EmptyStore())
    payload = hybrid.search_context(
        "kb", "SyncEngine", mode="graph", max_results=5, graph=_sample_graph()
    )
    assert payload["mode"] == "graph"
    assert payload["counts"]["graph"] >= 1
    assert any(r["node_id"] == "symbol:repo:SyncEngine" for r in payload["results"])


def test_hybrid_without_graph_falls_back_to_text() -> None:
    hybrid = HybridSearch(_EmptyStore())
    payload = hybrid.search_context("kb", "SyncEngine", mode="hybrid", max_results=5)
    assert payload["counts"]["graph"] == 0
    assert payload["results"] == []
