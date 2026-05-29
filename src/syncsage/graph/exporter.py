from __future__ import annotations

from syncsage.graph.simple import SimpleMultiDiGraph


def node_link(
    graph: SimpleMultiDiGraph,
    node_limit: int | None = None,
    link_limit: int | None = None,
) -> dict:
    if node_limit is None and link_limit is None:
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
    for node_id in graph.nodes:
        if len(nodes) >= max_nodes:
            break
        node = dict(graph.nodes[node_id])
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
        "truncated": len(nodes) < total_nodes or len(links) < total_links,
    }


def cytoscape(graph: SimpleMultiDiGraph) -> dict:
    elements = {"nodes": [{"data": data} for data in graph.to_node_link()["nodes"]], "edges": []}
    for edge in graph.to_node_link()["links"]:
        elements["edges"].append({"data": edge})
    return {"elements": elements}
