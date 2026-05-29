"""Acceptance tests for the web-UI-facing HTTP routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from syncsage.api.app import create_app, graph_neighbors, graph_slice
from syncsage.graph.simple import SimpleMultiDiGraph


def _client(config) -> TestClient:
    # No `with` block: the lifespan startup sync is skipped, keeping tests fast.
    return TestClient(create_app(config=config))


def test_config_route_returns_effective_and_profiles(loaded_config) -> None:
    client = _client(loaded_config)
    response = client.get("/config")
    assert response.status_code == 200
    payload = response.json()
    assert "effective" in payload
    assert payload["effective"]["syncsage"]["name"] == "acceptance-knowledge"
    assert "quickstart" in payload["profiles"]


def test_graph_routes_have_stable_shape_when_empty(loaded_config) -> None:
    client = _client(loaded_config)
    neighbors = client.get("/graph/neighbors", params={"node_id": "missing"})
    assert neighbors.status_code == 200
    assert neighbors.json()["neighbors"] == []

    explain = client.get("/nodes/explain", params={"node_id": "missing"})
    assert explain.status_code == 200
    assert "not present" in explain.json()["explanation"]

    slice_resp = client.get("/graph/slice", params={"node_id": "missing"})
    assert slice_resp.status_code == 200
    assert slice_resp.json()["links"] == []


def test_graph_route_can_return_bounded_preview(loaded_config) -> None:
    app = create_app(config=loaded_config)
    graph = app.state.engine.graph_builder.graph
    existing_nodes = graph.number_of_nodes()
    existing_links = graph.number_of_edges()
    graph.add_node("a", id="a", type="source", label="A")
    graph.add_node("b", id="b", type="file", label="B")
    graph.add_node("c", id="c", type="chunk", label="C")
    graph.add_edge("a", "b", type="contains")
    graph.add_edge("b", "c", type="has_chunk")

    response = TestClient(app).get("/graph", params={"limit": 2, "link_limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_nodes"] == existing_nodes + 3
    assert payload["total_links"] == existing_links + 2
    assert payload["truncated"] is True
    assert len(payload["nodes"]) == 2
    assert len(payload["links"]) <= 1


def test_fs_list_lists_roots_and_rejects_escapes(loaded_config, workspace_copy: Path) -> None:
    loaded_config.syncsage.workspace_root = workspace_copy
    loaded_config.security.allow_user_selected_source_paths = False
    client = _client(loaded_config)

    roots = client.get("/fs/list")
    assert roots.status_code == 200
    assert any(entry["path"] == str(workspace_copy) for entry in roots.json()["entries"])

    listing = client.get("/fs/list", params={"path": str(workspace_copy)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(workspace_copy)

    escaped = client.get("/fs/list", params={"path": "/etc"})
    assert escaped.status_code == 403


def test_fs_list_and_register_accept_user_selected_path(loaded_config, tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "note.md").write_text("# External\n", encoding="utf-8")
    loaded_config.syncsage.workspace_root = tmp_path / "workspace"
    loaded_config.security.allow_user_selected_source_paths = True
    client = _client(loaded_config)

    listing = client.get("/fs/list", params={"path": str(external)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(external.resolve())

    response = client.post(
        "/sources",
        json={
            "name": "external-notes",
            "type": "markdown_folder",
            "path": str(external),
            "max_depth": 0,
            "include": ["**/*.md"],
            "sync": {"on_startup": False},
        },
    )
    assert response.status_code == 200
    source = response.json()["source"]
    assert source["path"] == str(external.resolve())
    assert source["max_depth"] == 0


def test_register_source_appears_in_listing(loaded_config, workspace_copy: Path) -> None:
    loaded_config.syncsage.workspace_root = workspace_copy
    client = _client(loaded_config)
    target = workspace_copy / "notes"

    response = client.post(
        "/sources",
        json={
            "name": "ui-added-notes",
            "type": "markdown_folder",
            "path": str(target),
            "description": "Added through the UI",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "registered"
    assert body["config_update_required"] is True

    listed = client.get("/sources").json()
    assert any(source["name"] == "ui-added-notes" for source in listed)


def test_register_source_rejects_path_outside_roots(loaded_config, workspace_copy: Path) -> None:
    loaded_config.syncsage.workspace_root = workspace_copy
    loaded_config.security.allow_user_selected_source_paths = False
    client = _client(loaded_config)
    response = client.post(
        "/sources",
        json={"name": "escape", "type": "markdown_folder", "path": "/etc"},
    )
    assert response.status_code == 400


def test_update_source_persists_custom_runtime_config(loaded_config, workspace_copy: Path) -> None:
    loaded_config.syncsage.workspace_root = workspace_copy
    client = _client(loaded_config)
    response = client.put(
        "/sources/syncsage-repo",
        json={
            "path": str(workspace_copy / "syncsage-repo"),
            "type": "repository",
            "max_depth": 1,
            "include": ["**/*.py"],
            "exclude": ["**/.git/**"],
            "chunking": {"strategy": "semantic", "max_chars": 1200, "overlap_chars": 120},
            "sync": {"on_startup": False, "on_file_change": False, "on_git_commit": False},
            "repo": {"branch_policy": "current", "include_uncommitted": False},
        },
    )

    assert response.status_code == 200
    source = response.json()["source"]
    assert source["max_depth"] == 1
    assert source["chunking"]["max_chars"] == 1200
    assert source["repo"]["include_uncommitted"] is False


def test_node_content_route_returns_full_indexed_text(loaded_config) -> None:
    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    rows = app.state.state.rows(
        "SELECT artifact_id, text FROM chunks WHERE source_id=? ORDER BY chunk_index",
        ("architecture-notes",),
    )
    assert rows

    response = TestClient(app).get("/nodes/content", params={"node_id": rows[0]["artifact_id"]})

    assert response.status_code == 200
    assert rows[0]["text"].replace("\r\n", "\n") in response.json()["content"].replace("\r\n", "\n")


def test_promote_source_generates_patch(loaded_config, workspace_copy: Path) -> None:
    loaded_config.syncsage.workspace_root = workspace_copy
    client = _client(loaded_config)
    response = client.post("/sources/syncsage-repo/promote", json={"write": False})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "patch_generated"
    assert "sources:" in body["yaml_patch"]


def test_put_config_validates_and_writes(loaded_config, tmp_path: Path) -> None:
    config_file = tmp_path / "written.yaml"
    client = TestClient(create_app(config=loaded_config, config_path=config_file))
    payload = {
        "config": {
            "syncsage": {"name": "rewritten"},
            "server": {"port": 9001},
        }
    }
    response = client.put("/config", json=payload)
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert config_file.exists()
    assert "rewritten" in config_file.read_text(encoding="utf-8")


def test_put_config_rejects_non_mapping(loaded_config) -> None:
    client = _client(loaded_config)
    response = client.put("/config", json={"yaml_text": "- just\n- a\n- list\n"})
    assert response.status_code == 400


def test_pure_graph_helpers_traverse_typed_edges() -> None:
    graph = SimpleMultiDiGraph()
    graph.add_node("a", id="a", type="source", label="A")
    graph.add_node("b", id="b", type="file", label="B")
    graph.add_node("c", id="c", type="concept", label="C")
    graph.add_edge("a", "b", type="contains")
    graph.add_edge("b", "c", type="mentions")

    one_hop = graph_neighbors(graph, "a", depth=1)
    assert {n["node_id"] for n in one_hop["neighbors"]} == {"b"}

    two_hop = graph_neighbors(graph, "a", depth=2)
    assert {n["node_id"] for n in two_hop["neighbors"]} == {"b", "c"}

    filtered = graph_neighbors(graph, "a", depth=2, edge_types=["contains"])
    assert {n["node_id"] for n in filtered["neighbors"]} == {"b"}

    sliced = graph_slice(graph, "a", depth=2)
    assert {node["id"] for node in sliced["nodes"]} == {"a", "b", "c"}
    assert len(sliced["links"]) == 2
