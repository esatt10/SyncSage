from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class NodeView:
    def __init__(self, graph):
        self.graph = graph

    def __getitem__(self, key):
        with self.graph._lock:
            return self.graph._nodes[key]

    def __iter__(self):
        # Snapshot the keys: a reader thread must never hold a live iterator
        # over ``_nodes`` while the sync thread adds or removes nodes.
        with self.graph._lock:
            return iter(tuple(self.graph._nodes))

    def __contains__(self, key):
        return key in self.graph._nodes

    def __call__(self, data: bool = False):
        with self.graph._lock:
            return list(self.graph._nodes.items()) if data else list(self.graph._nodes)

    def get(self, key, default=None):
        with self.graph._lock:
            return self.graph._nodes.get(key, default)


class SimpleMultiDiGraph:
    """Minimal multi-digraph backing the knowledge graph.

    The graph is written by the sync thread (startup index, watcher and
    scheduler beats) while the HTTP API, the MCP tools and the assistant read
    it from their own threads. Every mutation and every read primitive
    therefore takes ``_lock``, and readers are handed *snapshots* rather than
    live dict views — otherwise a sync running underneath a ``/graph`` or
    ``/overview`` request blows up with "dictionary changed size during
    iteration". Use :meth:`reading` to keep a multi-call read consistent.
    """

    def __init__(self):
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
        # Out-adjacency, kept in step with ``_edges``. Without it every
        # ``out_edges`` call scanned the whole edge table, so a breadth-first
        # walk cost O(nodes × edges) — 80s for a two-hop slice of a 500k-edge
        # graph, and minutes at three. Insertion-ordered dicts (not sets) keep
        # traversal order deterministic, which decides which neighbours survive
        # a caller's limit.
        self._out: dict[str, dict[str, None]] = {}
        # Re-entrant: the writer path nests reads inside writes (upsert_node
        # reads created_at, graph_store.save serializes) on the same thread.
        self._lock = threading.RLock()
        self.nodes = NodeView(self)

    def reading(self):
        """Context manager pinning the graph for the duration of a read.

        ``with graph.reading():`` keeps counts and contents of one export
        consistent with each other; the individual primitives are safe on
        their own without it.
        """

        return self._lock

    def __contains__(self, node):
        return node in self._nodes

    def __len__(self):
        return len(self._nodes)

    def has_node(self, node):
        return node in self._nodes

    def add_node(self, node, **attrs):
        with self._lock:
            self._nodes[node] = {**self._nodes.get(node, {}), **attrs}

    def add_edge(self, source, target, **attrs):
        with self._lock:
            data = self._edges[(source, target)]
            data[len(data)] = attrs
            self._out.setdefault(source, {})[target] = None

    def remove_nodes_from(self, nodes):
        remove = set(nodes)
        with self._lock:
            for node in remove:
                self._nodes.pop(node, None)
                self._out.pop(node, None)
            for edge in list(self._edges):
                if edge[0] in remove or edge[1] in remove:
                    self._edges.pop(edge, None)
                    self._drop_adjacency(*edge)

    def remove_edges_from(self, edges):
        with self._lock:
            for source, target in edges:
                self._edges.pop((source, target), None)
                self._drop_adjacency(source, target)

    def _drop_adjacency(self, source: str, target: str) -> None:
        """Unlink one endpoint pair. Caller holds the lock."""

        targets = self._out.get(source)
        if targets is None:
            return
        targets.pop(target, None)
        if not targets:
            self._out.pop(source, None)

    def get_edge_data(self, source, target, default=None):
        with self._lock:
            found = self._edges.get((source, target))
            return default if found is None else found

    def edges(self):
        """Snapshot of ``((source, target), {key: data})`` for every edge.

        Edge attribute dicts are copied because ``GraphBuilder.upsert_edge``
        updates them in place on the sync thread.
        """

        with self._lock:
            return [
                (endpoints, {key: dict(data) for key, data in edge_map.items()})
                for endpoints, edge_map in self._edges.items()
            ]

    def neighbors(self, node):
        with self._lock:
            return list(self._out.get(node, ()))

    def out_edges(self, node):
        """Outgoing edges of one node — O(degree) via the adjacency index."""

        with self._lock:
            edges = []
            for target in self._out.get(node, ()):
                edge_map = self._edges.get((node, target))
                if not edge_map:
                    continue
                edges.append((node, target, {key: dict(data) for key, data in edge_map.items()}))
            return edges

    def number_of_nodes(self):
        return len(self._nodes)

    def number_of_edges(self):
        with self._lock:
            return sum(len(v) for v in self._edges.values())

    def to_node_link(self):
        with self._lock:
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
