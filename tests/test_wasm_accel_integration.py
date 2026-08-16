"""Step 34.5 acceptance: production wiring — config flag, and fallback.

Two properties matter beyond `test_wasm_accel.py`'s function-level parity:

1. Turning the flag on produces the identical end-to-end result as leaving
   it off (a real sync / a real search_graph call, not just the bare
   function).
2. A WASM failure at runtime falls back to the pure-Python path rather
   than breaking the sync or dropping search results — acceleration must
   never be a correctness dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("wasmtime", reason="wasmtime not installed (pip install 'pheasant-kb[wasm]')")

from pheasant.config.schema import PheasantConfig  # noqa: E402
from pheasant.graph.simple import SimpleMultiDiGraph  # noqa: E402
from pheasant.search.graph_search import search_graph  # noqa: E402
from pheasant.sync.engine import SyncEngine  # noqa: E402


def _make_engine(tmp_path: Path, sources: list[dict[str, Any]], *, wasm: bool) -> SyncEngine:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "wasm-accel-acceptance",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "ws"),
                "exports_path": str(tmp_path / "exports"),
            },
            "graph": {"wasm_cross_source_resolution": wasm},
            "sources": sources,
        }
    )
    return SyncEngine(config)


def _build_two_source_workspace(tmp_path: Path, *, wasm: bool) -> SyncEngine:
    code = tmp_path / "ws" / "code"
    code.mkdir(parents=True)
    (code / "app.py").write_text(
        "import shared.helpers\n\ndef run() -> None:\n    shared.helpers.go()\n",
        encoding="utf-8",
    )
    (code / "guide.md").write_text(
        "# Guide\n\nSee the [overview](product-overview.md) for context.\n",
        encoding="utf-8",
    )
    lib = tmp_path / "ws" / "lib"
    (lib / "shared").mkdir(parents=True)
    (lib / "shared" / "helpers.py").write_text(
        "def go() -> None:\n    return None\n", encoding="utf-8"
    )
    (lib / "product-overview.md").write_text("# Product Overview\n\nDetails.\n", encoding="utf-8")

    engine = _make_engine(
        tmp_path,
        [
            {"name": "code", "type": "repository", "path": str(code)},
            {"name": "lib", "type": "document_folder", "path": str(lib)},
        ],
        wasm=wasm,
    )
    engine.sync_source("lib", "full")
    engine.sync_source("code", "full")
    return engine


def _cross_source_edges(engine: SyncEngine) -> set[tuple[str, str, str]]:
    edges = engine.graph_builder.graph.to_node_link()["links"]
    return {(e["source"], e["target"], e["type"]) for e in edges if e.get("cross_source")}


def test_cross_source_resolution_flag_produces_identical_edges(tmp_path: Path) -> None:
    baseline = _build_two_source_workspace(tmp_path / "off", wasm=False)
    accelerated = _build_two_source_workspace(tmp_path / "on", wasm=True)

    baseline_edges = _cross_source_edges(baseline)
    accelerated_edges = _cross_source_edges(accelerated)
    assert baseline_edges, "fixture must actually produce cross-source edges"
    assert baseline_edges == accelerated_edges


def test_cross_source_resolution_falls_back_to_python_on_wasm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pheasant.sandbox.accel as accel_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated WASM failure")

    monkeypatch.setattr(accel_module, "resolve_cross_source_edges_wasm", _boom)
    # graph/builder.py imports this name at call time via
    # `from pheasant.sandbox.accel import resolve_cross_source_edges_wasm`,
    # so patch it there too (the name has already been bound at that point
    # for any prior import, but the deferred `from ... import` in
    # GraphBuilder._resolve_cross_source_edges re-resolves it fresh from
    # the accel package each call, so patching the package attribute above
    # is sufficient).

    accelerated_with_failure = _build_two_source_workspace(tmp_path / "fallback", wasm=True)
    baseline = _build_two_source_workspace(tmp_path / "baseline", wasm=False)

    assert _cross_source_edges(accelerated_with_failure) == _cross_source_edges(baseline)
    assert _cross_source_edges(baseline), "fixture must actually produce cross-source edges"


def _build_relationship_graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node("file:a:mod_a.py", label="mod_a.py", type="file")
    graph.add_node("file:b:mod_b.py", label="mod_b.py", type="file")
    graph.add_edge(
        "file:a:mod_a.py",
        "file:b:mod_b.py",
        type="imports",
        source_id="a",
        reference="pkg.mod_b",
    )
    return graph


def test_search_graph_wasm_flag_produces_identical_results() -> None:
    graph = _build_relationship_graph()
    query = "mod_b imports"

    baseline = search_graph(graph, query, max_results=10, wasm_relationship_search=False)
    accelerated = search_graph(graph, query, max_results=10, wasm_relationship_search=True)

    baseline_rel = [r for r in baseline if r.get("kind") == "relationship"]
    accelerated_rel = [r for r in accelerated if r.get("kind") == "relationship"]
    assert baseline_rel, "fixture must actually produce relationship hits"
    assert baseline_rel == accelerated_rel


def test_search_graph_falls_back_to_python_on_wasm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import pheasant.sandbox.accel as accel_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated WASM failure")

    monkeypatch.setattr(accel_module, "scan_edges_wasm", _boom)

    graph = _build_relationship_graph()
    query = "mod_b imports"
    baseline = search_graph(graph, query, max_results=10, wasm_relationship_search=False)
    with_failure = search_graph(graph, query, max_results=10, wasm_relationship_search=True)

    baseline_rel = [r for r in baseline if r.get("kind") == "relationship"]
    with_failure_rel = [r for r in with_failure if r.get("kind") == "relationship"]
    assert baseline_rel, "fixture must actually produce relationship hits"
    assert baseline_rel == with_failure_rel


def test_config_defaults_leave_wasm_acceleration_off() -> None:
    config = PheasantConfig.model_validate({"pheasant": {"name": "defaults-check"}})
    assert config.graph.wasm_cross_source_resolution is False
    assert config.search.wasm_relationship_search is False
