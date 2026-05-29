from __future__ import annotations

from pathlib import Path

from syncsage.mcp_server.tools import SyncSageTools
from tests.conftest import result_items, run_sync


def test_full_sync_creates_enriched_graph_nodes_and_edges(
    workspace_copy: Path,
    loaded_config: object,
    sync_engine: object,
) -> None:
    readme = workspace_copy / "syncsage-repo" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nSee [API docs](https://example.com/syncsage/api) and [@syncsage-paper].\n",
        encoding="utf-8",
    )
    (workspace_copy / "syncsage-repo" / "syncsage" / "cross_file.py").write_text(
        "import json\n\n"
        "HEALTH_PATH = '/health'\n\n"
        "class SearchAgent:\n"
        "    def render(self) -> str:\n"
        "        return json.dumps({'path': HEALTH_PATH})\n",
        encoding="utf-8",
    )

    run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    graph = sync_engine.graph_builder.graph
    node_types = {node["type"] for node in graph.to_node_link()["nodes"]}
    edge_types = {edge["type"] for edge in graph.to_node_link()["links"]}

    assert {"directory", "symbol", "entity", "concept", "external_reference"} <= node_types
    assert any(
        node["type"] == "directory" and node.get("relative_path") == "syncsage"
        for node in graph.to_node_link()["nodes"]
    )
    expected_edges = {
        "contains",
        "imports",
        "calls",
        "references",
        "derived_from",
        "mentions",
        "similar_to",
    }
    assert expected_edges <= edge_types


def test_graph_neighbors_honor_depth_and_edge_filters(
    loaded_config: object,
    sync_engine: object,
) -> None:
    run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    tools = SyncSageTools(loaded_config)
    result = tools.get_graph_neighbors(
        loaded_config.knowledge_base_id,
        "file:syncsage-repo:syncsage/sync_engine.py:branch=none",
        depth=2,
        edge_types=["mentions", "derived_from"],
    )

    assert any(neighbor["depth"] == 2 for neighbor in result["neighbors"])
    assert any(
        neighbor["depth"] == 2
        and neighbor["node"].get("relative_path") == "README.md"
        for neighbor in result["neighbors"]
    )


def test_graph_terms_improve_cross_file_search(
    sync_engine: object,
) -> None:
    run_sync(sync_engine, source_name="syncsage-repo", mode="full")

    search_result = sync_engine.search_context("SyncEngine HEALTH_PATH", max_results=5)
    paths = {
        item["relative_path"]
        for item in result_items(search_result)
        if isinstance(item, dict)
    }

    assert "syncsage/sync_engine.py" in paths
    assert "syncsage/api.py" in paths
