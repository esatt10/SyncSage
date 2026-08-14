from __future__ import annotations

import json
import threading
from collections import defaultdict
from typing import Any


def _search_blob(attrs: dict[str, Any]) -> str:
    """Every attribute value of a node, lowercased into one searchable string.

    A superset of what the scorer reads, deliberately: the blob is only ever
    used to *reject* nodes that cannot match, so covering extra fields can
    only cost a little scoring work, never a missed hit.
    """

    parts = []
    for value in attrs.values():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)):
            parts.append(str(value))
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.extend(str(item) for item in value.values())
    return " ".join(parts).lower()


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
        # Running aggregates. Summing 850k edge buckets or re-counting 240k
        # node types per request was costing seconds on endpoints the UI polls;
        # maintaining them on write makes those reads O(1).
        self._edge_count = 0
        self._type_counts: dict[str, int] = {}
        # Lowercased concatenation of every node attribute value, built on
        # write. Graph-mode search can only score a node above zero if a query
        # token appears somewhere in its text, so this lets a query reject the
        # overwhelming majority of nodes with one substring test instead of
        # stringifying and weighting every attribute of every node.
        self._search_blobs: dict[str, str] = {}
        # Nodes written or removed since the search index last flushed. The
        # index is a derived cache, so this is only bookkeeping for "what does
        # it still owe"; losing it costs a rebuild, never correctness.
        self._dirty_nodes: set[str] = set()
        self._removed_nodes: set[str] = set()
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
            existing = self._nodes.get(node)
            merged = {**(existing or {}), **attrs}
            self._nodes[node] = merged
            self._retype(existing.get("type") if existing else None, merged.get("type"))
            self._search_blobs[node] = _search_blob(merged)
            self._dirty_nodes.add(node)
            self._removed_nodes.discard(node)

    def add_edge(self, source, target, **attrs):
        with self._lock:
            data = self._edges[(source, target)]
            data[len(data)] = attrs
            self._edge_count += 1
            self._out.setdefault(source, {})[target] = None

    def _retype(self, old: Any, new: Any) -> None:
        """Keep the per-type tally in step with a node write. Lock held."""

        if old == new:
            return
        if old is not None:
            remaining = self._type_counts.get(str(old), 0) - 1
            if remaining > 0:
                self._type_counts[str(old)] = remaining
            else:
                self._type_counts.pop(str(old), None)
        if new is not None:
            self._type_counts[str(new)] = self._type_counts.get(str(new), 0) + 1

    def remove_nodes_from(self, nodes):
        remove = set(nodes)
        with self._lock:
            for node in remove:
                existing = self._nodes.pop(node, None)
                if existing is not None:
                    self._retype(existing.get("type"), None)
                self._search_blobs.pop(node, None)
                self._dirty_nodes.discard(node)
                if existing is not None:
                    self._removed_nodes.add(node)
                self._out.pop(node, None)
            for edge in list(self._edges):
                if edge[0] in remove or edge[1] in remove:
                    dropped = self._edges.pop(edge, None)
                    self._edge_count -= len(dropped or ())
                    self._drop_adjacency(*edge)

    def remove_edges_from(self, edges):
        with self._lock:
            for source, target in edges:
                dropped = self._edges.pop((source, target), None)
                self._edge_count -= len(dropped or ())
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
        return self._edge_count

    def type_counts(self) -> dict[str, int]:
        """Node count per type, maintained on write (was a full scan)."""

        with self._lock:
            return dict(self._type_counts)

    # --- lock-held iteration ------------------------------------------------
    # Snapshots are the safe default, but copying 240k nodes / 850k edges per
    # request cost seconds on the endpoints the canvas polls. These hand back
    # the live mappings for callers that hold ``reading()`` for the whole walk
    # — no copying, and the writer still cannot mutate underneath them.

    def iter_nodes(self):
        """``(node_id, attrs)`` pairs. Caller MUST hold ``reading()``."""

        return self._nodes.items()

    def search_blobs(self):
        """Live id→searchable-text mapping. Caller MUST hold ``reading()``."""

        return self._search_blobs

    def index_rows(self) -> list[tuple[str, str, str]]:
        """Every node as ``(node_id, source_id, body)`` for the search index."""

        with self._lock:
            return [
                (node_id, str(attrs.get("source_id") or ""), self._search_blobs.get(node_id, ""))
                for node_id, attrs in self._nodes.items()
            ]

    def take_index_delta(self) -> tuple[list[tuple[str, str, str]], list[str]]:
        """Claim what the search index still owes: ``(upserts, removals)``.

        Claiming clears the pending sets, so a failed flush loses updates
        rather than repeating them — acceptable because the index is a cache
        and a rebuild restores it exactly.
        """

        with self._lock:
            upserts = [
                (
                    node_id,
                    str((self._nodes.get(node_id) or {}).get("source_id") or ""),
                    self._search_blobs.get(node_id, ""),
                )
                for node_id in self._dirty_nodes
                if node_id in self._nodes
            ]
            removals = list(self._removed_nodes)
            self._dirty_nodes.clear()
            self._removed_nodes.clear()
            return upserts, removals

    def node_map(self):
        """The live id→attrs mapping. Caller MUST hold ``reading()``.

        For lock-held scans that look up many nodes: going through
        ``nodes.get`` re-acquires the lock per lookup, which is measurable
        when the scan touches every edge endpoint.
        """

        return self._nodes

    def iter_edges(self):
        """``((source, target), {key: attrs})``. Caller MUST hold ``reading()``."""

        return self._edges.items()

    def to_node_link(self):
        with self._lock:
            links = []
            for source, target in sorted(self._edges):
                # Edge keys are insertion-order implementation details and are
                # discarded by ``from_node_link``. Canonicalize parallel edges
                # by their semantic payload so concurrent source/file workers
                # cannot change persisted graph bytes merely by finishing in a
                # different order.
                edge_rows = sorted(
                    self._edges[(source, target)].values(),
                    key=lambda data: json.dumps(data, sort_keys=True, separators=(",", ":")),
                )
                for key, data in enumerate(edge_rows):
                    links.append({"source": source, "target": target, "key": key, **data})
            return {
                "directed": True,
                "multigraph": True,
                "graph": {},
                "nodes": [self._nodes[node_id] for node_id in sorted(self._nodes)],
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
