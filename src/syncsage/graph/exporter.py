from __future__ import annotations

from syncsage.graph.simple import SimpleMultiDiGraph


def node_link(
    graph: SimpleMultiDiGraph,
    node_limit: int | None = None,
    link_limit: int | None = None,
    node_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
    source_id: str | None = None,
) -> dict:
    """Node-link JSON, optionally bounded and filtered.

    Filtering happens *before* the limit is applied, so asking for a
    concept-free overview returns a full budget of structural nodes rather
    than a budget's worth of nodes that then mostly get dropped.
    ``total_nodes``/``total_links`` always report the unfiltered graph, so
    the caller can tell "filtered out" from "truncated away".
    """
    filtering = bool(node_types or exclude_node_types or source_id)

    def keep(node: dict) -> bool:
        if node_types and node.get("type") not in node_types:
            return False
        if exclude_node_types and node.get("type") in exclude_node_types:
            return False
        if source_id and node.get("source_id") not in (source_id, None):
            return False
        return True

    if node_limit is None and link_limit is None and not filtering:
        payload = graph.to_node_link()
        payload.update(
            {
                "total_nodes": graph.number_of_nodes(),
                "total_links": graph.number_of_edges(),
                "truncated": False,
            }
        )
        return payload

    total_nodes = graph.number_of_nodes()
    total_links = graph.number_of_edges()
    max_nodes = total_nodes if node_limit is None else max(0, node_limit)
    max_links = total_links if link_limit is None else max(0, link_limit)

    nodes = []
    selected_ids: set[str] = set()
    eligible = 0
    for node_id in graph.nodes:
        node = dict(graph.nodes[node_id])
        if not keep(node):
            continue
        eligible += 1
        if len(nodes) >= max_nodes:
            continue
        nodes.append(node)
        selected_ids.add(node_id)

    links = []
    for (source, target), edge_map in graph._edges.items():
        if source not in selected_ids or target not in selected_ids:
            continue
        for key, data in edge_map.items():
            if len(links) >= max_links:
                break
            links.append({"source": source, "target": target, "key": key, **data})
        if len(links) >= max_links:
            break

    return {
        "directed": True,
        "multigraph": True,
        "graph": {},
        "nodes": nodes,
        "links": links,
        "total_nodes": total_nodes,
        "total_links": total_links,
        "matched_nodes": eligible,
        "filtered": filtering,
        "truncated": len(nodes) < eligible,
    }


def cytoscape(graph: SimpleMultiDiGraph) -> dict:
    elements = {"nodes": [{"data": data} for data in graph.to_node_link()["nodes"]], "edges": []}
    for edge in graph.to_node_link()["links"]:
        elements["edges"].append({"data": edge})
    return {"elements": elements}
