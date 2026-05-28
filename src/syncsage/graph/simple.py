from __future__ import annotations

from collections import defaultdict
from typing import Any


class NodeView:
    def __init__(self, graph):
        self.graph = graph

    def __getitem__(self, key):
        return self.graph._nodes[key]

    def __iter__(self):
        return iter(self.graph._nodes)

    def __contains__(self, key):
        return key in self.graph._nodes

    def __call__(self, data: bool = False):
        return self.graph._nodes.items() if data else self.graph._nodes.keys()

class SimpleMultiDiGraph:
    def __init__(self):
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
        self.nodes = NodeView(self)

    def __contains__(self, node):
        return node in self._nodes

    def __len__(self):
        return len(self._nodes)

    def has_node(self, node):
        return node in self._nodes

    def add_node(self, node, **attrs):
        self._nodes[node] = {**self._nodes.get(node, {}), **attrs}

    def add_edge(self, source, target, **attrs):
        data = self._edges[(source, target)]
        data[len(data)] = attrs

    def remove_nodes_from(self, nodes):
        remove = set(nodes)
        for node in remove:
            self._nodes.pop(node, None)
        for edge in list(self._edges):
            if edge[0] in remove or edge[1] in remove:
                self._edges.pop(edge, None)

    def remove_edges_from(self, edges):
        for source, target in edges:
            self._edges.pop((source, target), None)

    def get_edge_data(self, source, target, default=None):
        return self._edges.get((source, target), default)

    def neighbors(self, node):
        for s, t in self._edges:
            if s == node:
                yield t

    def out_edges(self, node):
        for (source, target), edge_map in self._edges.items():
            if source == node:
                yield source, target, edge_map

    def number_of_nodes(self):
        return len(self._nodes)

    def number_of_edges(self):
        return sum(len(v) for v in self._edges.values())

    def to_node_link(self):
        links = []
        for (source, target), edge_map in self._edges.items():
            for key, data in edge_map.items():
                links.append({"source": source, "target": target, "key": key, **data})
        return {
            "directed": True,
            "multigraph": True,
            "graph": {},
            "nodes": list(self._nodes.values()),
            "links": links,
        }

    @classmethod
    def from_node_link(cls, data):
        graph = cls()
        for node in data.get("nodes", []):
            graph.add_node(node.get("id"), **node)
        for edge in data.get("links", []):
            attrs = dict(edge)
            source = attrs.pop("source")
            target = attrs.pop("target")
            attrs.pop("key", None)
            graph.add_edge(source, target, **attrs)
        return graph
