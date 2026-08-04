"""Step 34.5 acceptance: WASM-accelerated hot loops match their Python
originals exactly.

Every fixture here is asserted against the pure-Python function before
being trusted for anything else — the whole reason 34.4 didn't hand-author
this in raw WAT was the risk of a silent wrong-answer bug, so parity is
the load-bearing property, not an afterthought.
"""

from __future__ import annotations

import pytest

pytest.importorskip("wasmtime", reason="wasmtime not installed (pip install 'syncsage[wasm]')")

from syncsage.graph.enrichment import resolve_cross_source_edges  # noqa: E402
from syncsage.graph.simple import SimpleMultiDiGraph  # noqa: E402
from syncsage.sandbox.accel import (  # noqa: E402
    resolve_cross_source_edges_wasm,
    scan_edges_wasm,
)
from syncsage.search.graph_search import _scan_edges, _tokens  # noqa: E402


def _edge_key(edge):
    return (edge.source, edge.target, edge.type)


def _assert_cross_source_parity(nodes, edges) -> None:
    py_result = resolve_cross_source_edges(nodes, edges)
    wasm_result = resolve_cross_source_edges_wasm(nodes, edges)

    py_by_key = {_edge_key(e): e.attrs for e in py_result}
    wasm_by_key = {_edge_key(e): e.attrs for e in wasm_result}
    assert py_by_key.keys() == wasm_by_key.keys()
    for key in py_by_key:
        assert py_by_key[key] == wasm_by_key[key], f"attrs mismatch for {key}"
    assert [_edge_key(e) for e in py_result] == [_edge_key(e) for e in wasm_result], (
        "ordering must match too"
    )


def test_python_import_parity() -> None:
    nodes = [
        (
            "file:a:pkg/mod_a.py",
            {"type": "file", "relative_path": "pkg/mod_a.py", "source_id": "a"},
        ),
        (
            "file:b:pkg/mod_b.py",
            {"type": "file", "relative_path": "pkg/mod_b.py", "source_id": "b"},
        ),
        (
            "ext:a:import:0",
            {"type": "external_reference", "source_id": "a", "reference": "pkg.mod_b"},
        ),
    ]
    edges = [("file:a:pkg/mod_a.py", "ext:a:import:0", "imports", "python_import")]
    _assert_cross_source_parity(nodes, edges)


def test_document_link_parity_direct_and_suffix_and_wiki_style() -> None:
    nodes = [
        (
            "file:a:docs/readme.md",
            {"type": "markdown_note", "relative_path": "docs/readme.md", "source_id": "a"},
        ),
        (
            "file:b:notes/readme.md",
            {"type": "markdown_note", "relative_path": "notes/readme.md", "source_id": "b"},
        ),
        # direct relative link
        (
            "ext:a:link:0",
            {"type": "external_reference", "source_id": "a", "reference": "./docs/readme.md"},
        ),
        # wiki-style link without extension -> .md fallback
        ("ext:a:link:1", {"type": "external_reference", "source_id": "a", "reference": "readme"}),
        # fragment + query stripped
        (
            "ext:a:link:2",
            {
                "type": "external_reference",
                "source_id": "a",
                "reference": "docs/readme.md#section?x=1",
            },
        ),
        # relative ../ prefix (Python lstrip("./") strips the whole char-set, not one "./" literal)
        (
            "ext:a:link:3",
            {"type": "external_reference", "source_id": "a", "reference": "../../docs/readme.md"},
        ),
        # skipped: http/https/mailto
        (
            "ext:a:link:4",
            {
                "type": "external_reference",
                "source_id": "a",
                "reference": "https://example.com/readme.md",
            },
        ),
        (
            "ext:a:link:5",
            {"type": "external_reference", "source_id": "a", "reference": "mailto:a@example.com"},
        ),
    ]
    edges = [
        ("file:a:docs/readme.md", "ext:a:link:0", "references", "document_link"),
        ("file:a:docs/readme.md", "ext:a:link:1", "references", "document_link"),
        ("file:a:docs/readme.md", "ext:a:link:2", "references", "document_link"),
        ("file:a:docs/readme.md", "ext:a:link:3", "references", "document_link"),
        ("file:a:docs/readme.md", "ext:a:link:4", "references", "document_link"),
        ("file:a:docs/readme.md", "ext:a:link:5", "references", "document_link"),
    ]
    _assert_cross_source_parity(nodes, edges)


def test_url_reference_type_parity() -> None:
    nodes = [
        ("file:a:x.md", {"type": "markdown_note", "relative_path": "x.md", "source_id": "a"}),
        ("file:b:y.md", {"type": "markdown_note", "relative_path": "y.md", "source_id": "b"}),
        ("ext:a:link:0", {"type": "external_reference", "source_id": "a", "reference": "y.md"}),
    ]
    edges = [("file:a:x.md", "ext:a:link:0", "references", "url")]
    _assert_cross_source_parity(nodes, edges)


def test_unresolvable_and_unknown_reference_types_parity() -> None:
    nodes = [
        ("file:a:x.py", {"type": "file", "relative_path": "x.py", "source_id": "a"}),
        (
            "ext:a:link:0",
            {"type": "external_reference", "source_id": "a", "reference": "nowhere.py"},
        ),
        (
            "ext:a:link:1",
            {"type": "external_reference", "source_id": "a", "reference": "pkg.nomodule"},
        ),
    ]
    edges = [
        ("file:a:x.py", "ext:a:link:0", "references", "document_link"),
        ("file:a:x.py", "ext:a:link:1", "imports", None),  # no reference_type at all
    ]
    _assert_cross_source_parity(nodes, edges)


def test_same_source_internal_resolution_parity() -> None:
    nodes = [
        ("file:a:pkg/a.py", {"type": "file", "relative_path": "pkg/a.py", "source_id": "a"}),
        ("file:a:pkg/b.py", {"type": "file", "relative_path": "pkg/b.py", "source_id": "a"}),
        ("ext:a:link:0", {"type": "external_reference", "source_id": "a", "reference": "pkg.b"}),
    ]
    edges = [("file:a:pkg/a.py", "ext:a:link:0", "imports", "python_import")]
    _assert_cross_source_parity(nodes, edges)


def _build_relationship_graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node("file:a:mod_a.py", label="mod_a.py", type="file")
    graph.add_node("file:b:mod_b.py", label="mod_b.py", type="file")
    graph.add_node("file:c:mod_c.py", label="mod_c.py", type="file")
    graph.add_edge(
        "file:a:mod_a.py",
        "file:b:mod_b.py",
        type="imports",
        source_id="a",
        reference="pkg.mod_b",
    )
    graph.add_edge(
        "file:b:mod_b.py",
        "file:c:mod_c.py",
        type="references",
        source_id="b",
        reference="mod_c",
    )
    return graph


def test_scan_edges_parity_basic_query() -> None:
    graph = _build_relationship_graph()
    query = "mod_b imports"
    tokens = _tokens(query)
    q = query.strip().lower()

    py_hits = _scan_edges(graph, tokens, q, None)
    wasm_hits = scan_edges_wasm(graph, tokens, q, None)

    py_sorted = sorted(
        (round(score, 6), payload["source"], payload["target"]) for score, payload in py_hits
    )
    wasm_sorted = sorted(
        (round(score, 6), payload["source"], payload["target"]) for score, payload in wasm_hits
    )
    assert py_sorted == wasm_sorted
    assert len(py_hits) == len(wasm_hits) > 0

    # Full payload shape must match too, not just the score.
    py_by_pair = {(p["source"], p["target"]): p for _s, p in py_hits}
    wasm_by_pair = {(p["source"], p["target"]): p for _s, p in wasm_hits}
    assert py_by_pair == wasm_by_pair


def test_scan_edges_parity_with_source_filter() -> None:
    graph = _build_relationship_graph()
    query = "mod"
    tokens = _tokens(query)
    q = query.strip().lower()

    py_hits = _scan_edges(graph, tokens, q, "a")
    wasm_hits = scan_edges_wasm(graph, tokens, q, "a")
    assert (
        {p["source"] for _s, p in py_hits}
        == {p["source"] for _s, p in wasm_hits}
        == {"file:a:mod_a.py"}
    )
    assert len(py_hits) == len(wasm_hits)


def test_scan_edges_parity_no_match() -> None:
    graph = _build_relationship_graph()
    query = "totally-unrelated-zzz"
    tokens = _tokens(query)
    q = query.strip().lower()

    py_hits = _scan_edges(graph, tokens, q, None)
    wasm_hits = scan_edges_wasm(graph, tokens, q, None)
    assert py_hits == [] and wasm_hits == []
