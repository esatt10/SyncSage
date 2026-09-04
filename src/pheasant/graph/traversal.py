"""Bounded walks over a knowledge graph.

Pure domain: a graph in, nodes out. No application context, no transport, no
knowledge of who is asking — which is what lets the HTTP surface, the MCP
surface and the assistant all call the same walk instead of the three
implementations that existed before (`api.app.graph_neighbors`,
`PheasantTools.get_graph_neighbors`, and `PheasantTools.get_graph_slice`'s own
partial third).

Each function takes the *serving* graph rather than reaching for one, because
on an API replica pointed at the graph service it is a remote proxy and not a
resident snapshot. The ``remote_*`` checks are the seam: when the graph is
remote, the walk happens where the graph lives.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph

#: Edges that describe structure rather than a shortcut. Walked first so a
#: bounded view shows the tree rather than a flat fan of artifacts.
HIERARCHY_EDGE_TYPES = ("contains",)

#: Hard ceiling on traversal depth, whatever a caller asks for. Ten hops off a
#: hub node is already most of the graph.
MAX_DEPTH = 10


def _hierarchy_first(out_edges: list) -> list:
    """Structural edges before shortcuts, order otherwise preserved."""

    def rank(entry) -> int:
        types = {data.get("type") for data in entry[2].values()}
        return 0 if types & set(HIERARCHY_EDGE_TYPES) else 1

    return sorted(out_edges, key=rank)


def neighbors(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    max_nodes: int | None = None,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict[str, Any]:
    """Breadth-first neighbour expansion.

    ``max_nodes`` stops the walk once that many neighbours are collected.
    Callers that only keep the first N (the canvas asks for a bounded horizon)
    must pass it: three hops off a hub node reaches most of the graph, and
    enumerating all of it to then throw nearly all of it away is what made a
    depth-3 slice take minutes. Truncation is in BFS order either way, so the
    kept set is identical — only the work is smaller.

    The MCP tool used to implement this again, without the hierarchy-first
    ordering, without ``max_nodes`` and without either exclusion — so the two
    surfaces returned different neighbours for the same node, in a different
    order, and only one of them could be bounded.
    """

    remote = getattr(graph, "remote_neighbors", None)
    if callable(remote):
        return remote(
            node_id=node_id,
            depth=depth,
            edge_types=edge_types,
            max_nodes=max_nodes,
            exclude_edge_types=sorted(exclude_edge_types) if exclude_edge_types else None,
            exclude_node_types=sorted(exclude_node_types) if exclude_node_types else None,
        )
    if node_id not in graph:
        return {"node_id": node_id, "depth": depth, "neighbors": []}
    max_depth = max(1, min(int(depth or 1), MAX_DEPTH))
    allowed = set(edge_types or [])
    queue: deque = deque([(node_id, 0, [node_id])])
    visited = {node_id}
    found: list[dict] = []
    while queue:
        if max_nodes is not None and len(found) >= max_nodes:
            break
        current, current_depth, path = queue.popleft()
        if current_depth >= max_depth:
            continue
        # Hierarchy first. A source node carries an `indexes` shortcut to every
        # one of its artifacts, so a plain fan-out spends the whole budget
        # jumping straight to files and the directory tree between them never
        # gets walked — the parent/child structure is present in the graph but
        # invisible in any bounded view of it.
        for _source, target, edge_map in _hierarchy_first(graph.out_edges(current)):
            matching = [
                data
                for data in edge_map.values()
                if (not allowed or data.get("type") in allowed)
                and not (exclude_edge_types and data.get("type") in exclude_edge_types)
            ]
            if not matching:
                continue
            # Types the caller hides are pruned here rather than after the
            # fetch: a concept-heavy graph otherwise spends the entire budget
            # on nodes the view is about to discard, and the structure the
            # caller actually asked for never fits.
            if exclude_node_types:
                target_type = graph.nodes.get(target, {}).get("type")
                if target_type in exclude_node_types:
                    continue
            next_depth = current_depth + 1
            # One entry per *node*, not per edge into it. `visited` guarded the
            # queue but not the append, so a node reachable by two paths was
            # listed twice — every consumer treats this as a node list, and
            # `slice_` built its `nodes` payload straight off it, so the canvas
            # received duplicate element ids. It also charged the same node to
            # the budget twice, cutting a bounded slice short of the structure
            # it was asked for. First sighting wins, which is BFS order and
            # therefore the shortest path — the same rule `depths` applies. No
            # edge is lost: `slice_` derives links from the graph itself, so
            # both parents still draw.
            if target in visited:
                continue
            visited.add(target)
            edge_type_values = sorted({data.get("type") for data in matching if data.get("type")})
            found.append(
                {
                    "node_id": target,
                    "depth": next_depth,
                    "edge_types": edge_type_values,
                    "path": [*path, target],
                    "node": dict(graph.nodes.get(target, {})),
                }
            )
            queue.append((target, next_depth, [*path, target]))
            # A single hub can have thousands of out-edges, so the budget has
            # to bind inside the fan-out, not just between hops.
            if max_nodes is not None and len(found) >= max_nodes:
                break
    return {"node_id": node_id, "depth": depth, "neighbors": found}


def slice_(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    limit: int = 100,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict[str, Any]:
    """Connected sub-graph around a node.

    Named with the trailing underscore because ``slice`` is a builtin; the
    surfaces expose it as ``graph_slice`` / ``get_graph_slice``, which are the
    names in their respective public contracts.
    """

    remote = getattr(graph, "remote_slice", None)
    if callable(remote):
        return remote(
            node_id=node_id,
            depth=depth,
            edge_types=edge_types,
            limit=limit,
            exclude_edge_types=sorted(exclude_edge_types) if exclude_edge_types else None,
            exclude_node_types=sorted(exclude_node_types) if exclude_node_types else None,
        )
    # Ask for one more neighbour than the caller will receive. Without that
    # sentinel a bounded UI slice silently looked complete whenever it filled
    # its budget, which made large documents appear to have lost chunks.
    neighbour_limit = max(0, int(limit))
    traversal = neighbors(
        graph,
        node_id,
        depth,
        edge_types,
        max_nodes=neighbour_limit + 1,
        exclude_edge_types=exclude_edge_types,
        exclude_node_types=exclude_node_types,
    )
    all_neighbors = traversal["neighbors"]
    kept = all_neighbors[:neighbour_limit]
    node_ids = [node_id] + [item["node_id"] for item in kept]
    node_set = set(node_ids)
    # Hop distance per node, nearest wins (BFS order, so the first sighting is
    # the shortest path). The UI rings the canvas by this and lets the user
    # widen the horizon a layer at a time instead of rendering the whole graph.
    depths: dict[str, int] = {node_id: 0}
    for item in kept:
        target = str(item["node_id"])
        hop = int(item.get("depth") or 0)
        if hop < depths.get(target, hop + 1):
            depths[target] = hop
    links = []
    allowed = set(edge_types or [])
    for source in node_set:
        if source not in graph:
            continue
        for _src, target, edge_map in graph.out_edges(source):
            if target not in node_set:
                continue
            for key, data in edge_map.items():
                if allowed and data.get("type") not in allowed:
                    continue
                links.append({"source": source, "target": target, "key": key, **data})
    return {
        "node_id": node_id,
        "depth": depth,
        "nodes": [dict(attrs) for attrs in (graph.nodes.get(item) for item in node_ids) if attrs],
        "links": links,
        "depths": depths,
        "truncated": len(all_neighbors) > neighbour_limit,
    }
