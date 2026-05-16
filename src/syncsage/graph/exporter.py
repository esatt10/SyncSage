from __future__ import annotations

from syncsage.graph.simple import SimpleMultiDiGraph


def node_link(graph: SimpleMultiDiGraph) -> dict:
    return graph.to_node_link()


def cytoscape(graph: SimpleMultiDiGraph) -> dict:
    elements = {"nodes": [{"data": data} for data in graph.to_node_link()["nodes"]], "edges": []}
    for edge in graph.to_node_link()["links"]:
        elements["edges"].append({"data": edge})
    return {"elements": elements}
