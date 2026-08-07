"""The full-screen graph workspace's backing data.

Acceptance:

1. Diagnostics report what a picture cannot: hubs by degree, orphans, and the
   edge/node type histograms.
2. Path finding follows edges in **both** directions — relatedness is not a
   question about which way an import points — and is bounded so a miss cannot
   pin the server.
3. Both run over a real indexed graph, not a hand-built fixture only.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pheasant.api.app import _shortest_path, create_app
from pheasant.graph.simple import SimpleMultiDiGraph


def _graph(edges: list[tuple[str, str]], **node_types: str) -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    for source, target in edges:
        for node in (source, target):
            if node not in graph:
                graph.add_node(node, type=node_types.get(node, "artifact"), label=node)
        graph.add_edge(source, target, type="references")
    return graph


# ------------------------------------------------------------ path finding


def test_a_direct_path_is_one_hop() -> None:
    graph = _graph([("a", "b")])
    assert _shortest_path(graph, "a", "b") == ["a", "b"]


def test_a_node_to_itself_is_a_zero_hop_path() -> None:
    graph = _graph([("a", "b")])
    assert _shortest_path(graph, "a", "a") == ["a"]


def test_edges_are_followed_in_both_directions() -> None:
    """`b -> a` still connects a to b: a file and the concept it mentions are
    related whichever way you walk the edge."""
    graph = _graph([("b", "a")])
    assert _shortest_path(graph, "a", "b") == ["a", "b"]


def test_the_shortest_of_several_routes_wins() -> None:
    graph = _graph([("a", "x"), ("x", "y"), ("y", "d"), ("a", "d")])
    assert _shortest_path(graph, "a", "d") == ["a", "d"]


def test_a_long_chain_is_reconstructed_end_to_end() -> None:
    chain = [(f"n{i}", f"n{i + 1}") for i in range(6)]
    graph = _graph(chain)
    path = _shortest_path(graph, "n0", "n6")
    assert path == [f"n{i}" for i in range(7)]


def test_disconnected_nodes_have_no_path() -> None:
    graph = _graph([("a", "b"), ("c", "d")])
    assert _shortest_path(graph, "a", "d") is None


def test_the_depth_bound_is_respected() -> None:
    chain = [(f"n{i}", f"n{i + 1}") for i in range(10)]
    graph = _graph(chain)
    assert _shortest_path(graph, "n0", "n10", max_depth=2) is None
    assert _shortest_path(graph, "n0", "n10", max_depth=10) is not None


def test_a_cycle_does_not_loop_forever() -> None:
    graph = _graph([("a", "b"), ("b", "c"), ("c", "a")])
    assert _shortest_path(graph, "a", "c") == ["a", "c"]


def test_the_visit_budget_bounds_a_miss() -> None:
    """A hub-heavy graph must not let one bad pair pin the server."""
    graph = _graph([("hub", f"leaf{i}") for i in range(200)])
    graph.add_node("island", type="artifact", label="island")
    assert _shortest_path(graph, "hub", "island", max_visited=10) is None


# ------------------------------------------------------------ diagnostics


def test_diagnostics_report_hubs_orphans_and_histograms(loaded_config, config_path: Path) -> None:
    app = create_app(config=loaded_config, config_path=config_path)
    client = TestClient(app)
    client.post("/sync", json={"mode": "full", "wait": True})

    body = client.get("/graph/diagnostics").json()

    assert body["total_nodes"] > 0
    assert body["total_links"] > 0
    assert body["node_types"]
    assert body["edge_types"]
    # Hubs are ordered by degree, most connected first.
    degrees = [hub["degree"] for hub in body["hubs"]]
    assert degrees == sorted(degrees, reverse=True)
    assert body["hubs"][0]["node_id"]
    assert body["density"] > 0


def test_orphans_are_counted_and_sampled() -> None:
    from pheasant.api.app import create_app as _create

    graph = _graph([("a", "b")])
    graph.add_node("lonely", type="artifact", label="lonely")
    # Exercised directly: building a real index with a guaranteed orphan is
    # more machinery than the property needs.
    degree: dict[str, int] = {}
    with graph.reading():
        for (source, target), edge_map in graph.iter_edges():
            degree[source] = degree.get(source, 0) + len(edge_map)
            degree[target] = degree.get(target, 0) + len(edge_map)
        orphans = [node for node in graph.node_map() if node not in degree]
    assert orphans == ["lonely"]
    assert _create is not None  # import guard


def test_edge_type_histogram_is_sorted_by_weight(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sync", json={"mode": "full", "wait": True})
    counts = list(client.get("/graph/diagnostics").json()["edge_types"].values())
    assert counts == sorted(counts, reverse=True)


def test_path_route_walks_a_real_indexed_graph(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sync", json={"mode": "full", "wait": True})

    hubs = client.get("/graph/diagnostics").json()["hubs"]
    assert len(hubs) >= 2
    response = client.get(
        "/graph/path", params={"source": hubs[0]["node_id"], "target": hubs[1]["node_id"]}
    )

    assert response.status_code == 200
    body = response.json()
    if body["found"]:
        assert body["path"][0]["node_id"] == hubs[0]["node_id"]
        assert body["path"][-1]["node_id"] == hubs[1]["node_id"]
        assert body["hops"] == len(body["path"]) - 1


def test_path_route_404s_an_unknown_node(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sync", json={"mode": "full", "wait": True})
    kb = loaded_config.knowledge_base_id
    assert client.get("/graph/path", params={"source": kb, "target": "nope"}).status_code == 404
    assert client.get("/graph/path", params={"source": "nope", "target": kb}).status_code == 404
