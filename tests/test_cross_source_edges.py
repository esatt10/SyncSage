"""Synapse 21.6B: cross-source edges + concept normalization.

Offline, deterministic acceptance tests. They build small isolated
workspaces (no network) with >= 2 sources and assert the graph gains the
expected cross-source ``imports``/``references`` edges, that intra-source and
unresolvable references are unaffected, and that concept normalization
collapses singular/plural duplicates into a single ``concept`` node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from syncsage.config.schema import SyncSageConfig
from syncsage.graph.enrichment import (
    _normalize_concept,
    _singularize_word,
)
from syncsage.sync.engine import SyncEngine


def _make_engine(tmp_path: Path, sources: list[dict[str, Any]]) -> SyncEngine:
    config = SyncSageConfig.model_validate(
        {
            "syncsage": {
                "name": "cross-source-acceptance",
                "state_path": str(tmp_path / "state"),
                "vault_path": str(tmp_path / "vault"),
                "workspace_root": str(tmp_path / "ws"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": sources,
        }
    )
    return SyncEngine(config)


def _edges(engine: SyncEngine) -> list[dict[str, Any]]:
    return engine.graph_builder.graph.to_node_link()["links"]


def _node_type(engine: SyncEngine, node_id: str) -> str | None:
    nodes = {n["id"]: n for n in engine.graph_builder.graph.to_node_link()["nodes"]}
    node = nodes.get(node_id)
    return node.get("type") if node else None


def _build_two_source_workspace(tmp_path: Path) -> SyncEngine:
    # Source A: a code package that imports a module living in source B, and a
    # markdown note linking to a document that lives in source B.
    code = tmp_path / "ws" / "code"
    code.mkdir(parents=True)
    (code / "app.py").write_text(
        "import shared.helpers\n\n"
        "def run() -> None:\n"
        "    shared.helpers.go()\n",
        encoding="utf-8",
    )
    (code / "guide.md").write_text(
        "# Guide\n\nSee the [overview](product-overview.md) for context.\n"
        "Also see [missing](nowhere.md).\n",
        encoding="utf-8",
    )

    # Source B: provides shared/helpers.py and product-overview.md.
    lib = tmp_path / "ws" / "lib"
    (lib / "shared").mkdir(parents=True)
    (lib / "shared" / "helpers.py").write_text(
        "def go() -> None:\n    return None\n",
        encoding="utf-8",
    )
    (lib / "product-overview.md").write_text(
        "# Product Overview\n\nThe product overview document.\n",
        encoding="utf-8",
    )

    engine = _make_engine(
        tmp_path,
        [
            {"name": "code", "type": "repository", "path": str(code)},
            {"name": "lib", "type": "document_folder", "path": str(lib)},
        ],
    )
    engine.sync_source("lib", "full")
    engine.sync_source("code", "full")
    return engine


def test_cross_source_imports_edge_created(tmp_path: Path) -> None:
    engine = _build_two_source_workspace(tmp_path)
    edges = _edges(engine)

    cross = [
        e
        for e in edges
        if e.get("cross_source")
        and e["type"] == "imports"
        and e.get("source_id") == "code"
        and e.get("target_source_id") == "lib"
    ]
    assert cross, "expected a cross-source imports edge from code -> lib"
    edge = cross[0]
    assert _node_type(engine, edge["source"]) in {"file", "document", "markdown_note"}
    assert _node_type(engine, edge["target"]) in {"file", "document", "markdown_note"}
    assert "helpers.py" in edge["target"]


def test_cross_source_references_edge_created(tmp_path: Path) -> None:
    engine = _build_two_source_workspace(tmp_path)
    edges = _edges(engine)

    cross = [
        e
        for e in edges
        if e.get("cross_source")
        and e["type"] == "references"
        and e.get("source_id") == "code"
        and e.get("target_source_id") == "lib"
    ]
    assert cross, "expected a cross-source references edge from code -> lib"
    assert any("product-overview" in e["target"] for e in cross)


def test_unresolvable_reference_stays_external(tmp_path: Path) -> None:
    """A link with no resolving artifact keeps the external_reference behavior
    (no false cross-source edge, original references-to-external preserved)."""

    engine = _build_two_source_workspace(tmp_path)
    edges = _edges(engine)

    nodes = {n["id"]: n for n in engine.graph_builder.graph.to_node_link()["nodes"]}
    # The external_reference node for the unresolvable link must still exist.
    assert any(
        n["type"] == "external_reference" and "nowhere" in str(n.get("reference", ""))
        for n in nodes.values()
    )
    # ...and the artifact -> external_reference references edge must remain
    # (not mislabeled cross_source).
    external_ref_edges = [
        e
        for e in edges
        if e["type"] == "references"
        and _node_type(engine, e["target"]) == "external_reference"
        and not e.get("cross_source")
    ]
    assert any(
        "nowhere" in str(nodes[e["target"]].get("reference", "")) for e in external_ref_edges
    )
    # No cross-source edge ever resolved the missing target.
    assert not any(
        e.get("cross_source") and "nowhere" in str(e.get("reference", "")) for e in edges
    )


def test_no_cross_source_edge_for_same_source_resolution(tmp_path: Path) -> None:
    """An import resolving within the same source does not become a
    cross_source edge."""

    code = tmp_path / "ws" / "pkg"
    (code / "pkg").mkdir(parents=True)
    (code / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (code / "pkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (code / "main.py").write_text("import pkg.core\n", encoding="utf-8")

    engine = _make_engine(
        tmp_path, [{"name": "pkg", "type": "repository", "path": str(code)}]
    )
    engine.sync_source("pkg", "full")

    assert not any(e.get("cross_source") for e in _edges(engine))


def test_cross_source_edges_are_idempotent(tmp_path: Path) -> None:
    engine = _build_two_source_workspace(tmp_path)
    first_nodes = engine.graph_builder.graph.number_of_nodes()
    first_edges = engine.graph_builder.graph.number_of_edges()

    engine.sync_source("code", "full")
    engine.sync_source("lib", "full")
    engine.sync_source("code", "full")

    assert engine.graph_builder.graph.number_of_nodes() == first_nodes
    assert engine.graph_builder.graph.number_of_edges() == first_edges


def test_singularize_word_rules() -> None:
    assert _singularize_word("systems") == "system"
    assert _singularize_word("libraries") == "library"
    assert _singularize_word("boxes") == "box"
    assert _singularize_word("classes") == "class"  # …(s)es -> drop es
    assert _singularize_word("status") == "status"  # stoplisted
    assert _singularize_word("analysis") == "analysis"  # stoplisted
    assert _singularize_word("class") == "class"  # ends in ss
    assert _singularize_word("system") == "system"  # already singular (idempotent)
    assert _singularize_word("os") == "os"  # too short / stoplisted


def test_normalize_concept_collapses_plurals() -> None:
    assert (
        _normalize_concept("Systems")
        == _normalize_concept("systems")
        == _normalize_concept("system")
        == "system"
    )


def test_concept_normalization_collapses_duplicate_nodes(tmp_path: Path) -> None:
    """A workspace mentioning system / Systems / systems yields ONE concept
    node, fewer than the un-normalized (per-surface-form) baseline."""

    notes = tmp_path / "ws" / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text(
        "# Systems\n\nWe build systems. Systems are systems. A single System too.\n",
        encoding="utf-8",
    )
    # Two documents, because a concept node exists to link documents: with
    # graph.concept_min_documents at its default of 2, a term only one file
    # mentions is not kept as a node (it stays on the artifact's terms).
    (notes / "b.md").write_text(
        "# Related\n\nOur System design notes, describing systems.\n",
        encoding="utf-8",
    )
    engine = _make_engine(
        tmp_path, [{"name": "notes", "type": "markdown_folder", "path": str(notes)}]
    )
    engine.sync_source("notes", "full")

    concept_labels = [
        n["label"]
        for n in engine.graph_builder.graph.to_node_link()["nodes"]
        if n["type"] == "concept"
    ]
    system_concepts = [label for label in concept_labels if label in {"system", "systems"}]
    # All surface forms collapse to a single normalized "system" concept.
    assert system_concepts == ["system"]
    assert "systems" not in concept_labels
