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
        "import shared.helpers\n\ndef run() -> None:\n    shared.helpers.go()\n",
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

    engine = _make_engine(tmp_path, [{"name": "pkg", "type": "repository", "path": str(code)}])
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


def test_imports_resolve_to_files_inside_the_same_source() -> None:
    """The connectivity the graph existed for, and did not have.

    Per-artifact enrichment emits `imports` edges to an *external_reference*
    node named after the module. Nothing turned those into a link to the file
    that module actually is unless the target happened to live in a different
    source — so a single-source knowledge base, which is the common case, had
    zero file->file import edges. On the 2,132-file demo corpus that was 1,871
    `imports` edges every one of which ended at a name.
    """
    from syncsage.graph.enrichment import resolve_cross_source_edges

    nodes = [
        (
            "file:repo:pkg/runner.py",
            {"type": "file", "relative_path": "pkg/runner.py", "source_id": "repo"},
        ),
        (
            "file:repo:pkg/checkpoint.py",
            {"type": "file", "relative_path": "pkg/checkpoint.py", "source_id": "repo"},
        ),
        (
            "external:repo:pkg.checkpoint",
            {
                "type": "external_reference",
                "reference": "pkg.checkpoint",
                "source_id": "repo",
            },
        ),
    ]
    edges = [
        ("file:repo:pkg/runner.py", "external:repo:pkg.checkpoint", "imports", "python_import")
    ]

    resolved = resolve_cross_source_edges(nodes, edges)

    assert len(resolved) == 1
    edge = resolved[0]
    assert edge.source == "file:repo:pkg/runner.py"
    assert edge.target == "file:repo:pkg/checkpoint.py"
    assert edge.type == "imports"
    # Same-source resolution is labelled distinctly, so the 21.6B cross-source
    # feature stays observable and its own tests keep meaning what they said.
    assert edge.attrs["cross_source"] is False
    assert edge.attrs["enrichment_pass"] == "internal_resolution"


def test_internal_resolution_is_idempotent_and_skips_self_imports() -> None:
    from syncsage.graph.enrichment import resolve_cross_source_edges

    nodes = [
        (
            "file:repo:pkg/mod.py",
            {"type": "file", "relative_path": "pkg/mod.py", "source_id": "repo"},
        ),
        (
            "external:repo:pkg.mod",
            {"type": "external_reference", "reference": "pkg.mod", "source_id": "repo"},
        ),
    ]
    # A module importing itself (re-export shims do this) must not self-loop.
    edges = [("file:repo:pkg/mod.py", "external:repo:pkg.mod", "imports", "python_import")]
    assert resolve_cross_source_edges(nodes, edges) == []

    # And running twice yields the same edges, not duplicates.
    nodes.append(
        (
            "file:repo:pkg/other.py",
            {"type": "file", "relative_path": "pkg/other.py", "source_id": "repo"},
        )
    )
    edges.append(("file:repo:pkg/other.py", "external:repo:pkg.mod", "imports", "python_import"))
    first = resolve_cross_source_edges(nodes, edges)
    second = resolve_cross_source_edges(nodes, edges)
    assert len(first) == 1
    assert [(e.source, e.target, e.type) for e in first] == [
        (e.source, e.target, e.type) for e in second
    ]


def test_markdown_checkboxes_are_not_citations() -> None:
    """`- [x] done` is a checkbox, not a reference.

    The citation pattern captured every bracketed token, so task lists,
    changelog headings and version placeholders became `external_reference`
    nodes. Harmless while facts were buried under concepts; visible the moment
    the panel started surfacing what documents reference.
    """
    from syncsage.graph.enrichment import _citation_candidates

    text = (
        "- [x] shipped\n- [ ] pending\n## [Unreleased]\n"
        "Bump to [X.Y.Z] / [v1.2.3] / [1.0].\n"
        "As shown in [Smith2024] and [KernelFunction].\n"
    )
    assert _citation_candidates(text) == ["Smith2024", "KernelFunction"]


def test_citation_extraction_is_deterministic_and_deduped() -> None:
    from syncsage.graph.enrichment import _citation_candidates

    text = "[Alpha] then [alpha] then [Beta] then [Alpha]"
    first = _citation_candidates(text)
    assert first == _citation_candidates(text)
    assert first == ["Alpha", "Beta"]


def test_reference_label_does_not_crash_on_urlparse_edge_cases() -> None:
    """Found live indexing the real mlflow repo: a reference string shaped
    like `//[...` (no closing `]`) makes `urlparse` raise `ValueError:
    Invalid IPv6 URL` instead of returning an unparsed result the way it
    does for other malformed input — `_reference_label` is cosmetic only
    (node identity comes from `_node_id`), so it must fail open to the raw
    value rather than take the whole sync down."""
    from syncsage.graph.enrichment import _reference_label

    assert _reference_label("//[unterminated-bracket") == "//[unterminated-bracket"
    assert _reference_label("//user:pa[ss@host") == "//user:pa[ss@host"
    # Ordinary inputs are unaffected.
    assert _reference_label("https://example.com/path") == "example.com/path"
    assert _reference_label("pkg.module") == "pkg.module"


def test_full_sync_survives_a_reference_that_used_to_crash_urlparse(tmp_path: Path) -> None:
    """End-to-end: the same shape, indexed for real, must not fail the sync."""
    code = tmp_path / "ws" / "code"
    code.mkdir(parents=True)
    (code / "guide.md").write_text(
        "# Guide\n\nSee [broken](//[unterminated) for details.\n",
        encoding="utf-8",
    )
    engine = _make_engine(tmp_path, [{"name": "code", "type": "repository", "path": str(code)}])
    engine.sync_source("code", "full")  # must not raise
    assert any(
        n["type"] == "external_reference"
        for n in engine.graph_builder.graph.to_node_link()["nodes"]
    )
