from __future__ import annotations

from collections import defaultdict
from typing import Any

class NodeView:
    def __init__(self, graph): self.graph=graph
    def __getitem__(self, key): return self.graph._nodes[key]
    def __iter__(self): return iter(self.graph._nodes)
    def __contains__(self, key): return key in self.graph._nodes
    def __call__(self, data: bool=False): return self.graph._nodes.items() if data else self.graph._nodes.keys()

class SimpleMultiDiGraph:
    def __init__(self):
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[tuple[str,str], dict[int, dict[str, Any]]] = defaultdict(dict)
        self.nodes = NodeView(self)
    def __contains__(self, node): return node in self._nodes
    def __len__(self): return len(self._nodes)
    def has_node(self, node): return node in self._nodes
    def add_node(self, node, **attrs): self._nodes[node] = {**self._nodes.get(node, {}), **attrs}
    def add_edge(self, source, target, **attrs):
        data=self._edges[(source,target)]; data[len(data)] = attrs
    def get_edge_data(self, source, target, default=None): return self._edges.get((source,target), default)
    def neighbors(self, node):
        for s,t in self._edges:
            if s == node: yield t
    def number_of_nodes(self): return len(self._nodes)
    def number_of_edges(self): return sum(len(v) for v in self._edges.values())
    def to_node_link(self):
        links=[]
        for (s,t), edge_map in self._edges.items():
            for key,data in edge_map.items(): links.append({"source":s,"target":t,"key":key,**data})
        return {"directed": True, "multigraph": True, "graph": {}, "nodes": list(self._nodes.values()), "links": links}
    @classmethod
    def from_node_link(cls, data):
        g=cls()
        for n in data.get("nodes", []): g.add_node(n.get("id"), **n)
        for e in data.get("links", []):
            attrs=dict(e); s=attrs.pop("source"); t=attrs.pop("target"); attrs.pop("key", None); g.add_edge(s,t,**attrs)
        return g
