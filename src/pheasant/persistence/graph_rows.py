"""The knowledge graph as rows, written by delta (Phase 35.10).

The graph was one zstd node-link blob. Every commit re-serialized the whole
thing and every process that answered a graph query held the whole thing
resident. Measured with ``python -m pheasant.graph.capacity`` on this repo's
own synthetic corpus, at 100k files (630k nodes / 630k edges):

===================================  ==========  ==========
                                     file        rows
===================================  ==========  ==========
commit after a one-file change       9.1 s       0.010 s
load before a replica can serve      4.6 s       none
resident bytes to answer a query     1.5 GB      none
3-hop bounded traversal              in-RAM      0.026 ms
===================================  ==========  ==========

The commit number is the one that matters, and not because 9 seconds is slow:
it is **O(total graph)** where the change was O(one file), it runs while the
sync mutex is held, and one process is the sole commit authority per shard
(``sync/saturation.py``). That made corpus size, not write rate, the thing
that saturated the commit stream — and the only documented way past it was to
shard the region. As rows the same commit is 10 ms *and flat in graph size*.

**What this deliberately does not change.**

* The graph is still published as immutable generations with a
  content-addressed id. See :meth:`GraphRowStore.publish`.
* Stable IDs are untouched (CLAUDE.md rule 3). A node id is a node id; this
  changes where it is stored, not what it is.
* Snapshots stay files. They are history, they are read whole or not at all,
  and they are interval-gated rather than per-commit — so materializing one
  from rows at O(N) is the right cost in the right place.
* Nothing new is introduced. These are two tables in the state store the
  region already runs, in both dialects, next to the artifacts and chunks the
  same commit writes — which also means the graph and the chunks it describes
  now land in **one transaction** instead of a database write followed by a
  separate file rename.

**The generation id survives the move.** It is content-addressed, carries no
clock, and two replicas must name an unchanged graph identically without
coordinating (pillar 1). A digest over a serialized whole graph gave that for
free and cost O(N). Here every row carries its own digest and the published id
folds them with **XOR**, which is its own inverse: a commit folds each changed
row's old digest out and its new one in, so the published id is exact and the
update is O(changed). Each key contributes exactly once — both tables have a
primary key — so the multiset cancellation XOR set-hashes are criticized for
cannot arise. :meth:`recompute_folds` re-derives them with a full scan for
``sync --mode repair`` and for the tests, because an incrementally maintained
aggregate that nothing ever checks is an aggregate that drifts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Attributes promoted out of ``attrs`` into their own column. Chosen because
#: something filters or joins on each of them — not to make rows look tidy.
#: They are stored once: :func:`node_row` removes them from the JSON and
#: :func:`node_attrs` merges them back, so the projection lives in one place.
PROMOTED_NODE_KEYS = ("type", "label", "source_id", "relative_path", "artifact_id")

#: Same, for an edge. ``type`` and ``seq`` are part of the primary key.
PROMOTED_EDGE_KEYS = ("type", "source_id")

#: The composite key of ``graph_edges``, in its declared order. Named once
#: because it is both the primary key and the order every streaming read
#: pages by, and those two agreeing is what makes the paging a range scan.
EDGE_KEY = ("source", "target", "type", "seq")

#: Rows per statement. The same figure ``persistence/migrate.py`` uses, for the
#: same reason: a million-row table should not be a million round trips, and a
#: parameter list should not be measured in gigabytes.
BATCH = 1_000

#: Endpoint *sources* per statement when addressing edges by exact pair.
#:
#: Deliberately far smaller than :data:`BATCH`. Addressing pairs needs an
#: ``OR`` chain — ``source IN (…) AND target IN (…)`` is a cross product, which
#: over-matches, and over-matching here would fold out digests that were never
#: replaced and delete edges nobody touched. An ``OR`` chain nests one level
#: per term, and SQLite refuses an expression tree deeper than 1000 outright:
#: batching pairs at ``BATCH`` raised "Expression tree is too large" on the
#: first real graph. Grouping the targets under each source keeps the chain
#: proportional to *distinct sources*, and this caps that.
PAIR_GROUP_BATCH = 100

#: Targets listed per source inside one group. An ``IN`` list is one node in
#: the tree however long it is, so this bounds the parameter count rather than
#: the depth.
PAIR_TARGET_BATCH = 400

#: Hex digits of SHA-256 kept per row. 128 bits is far past what a collision
#: between two versions of one region's node needs, and the fold is stored as
#: text so it reads in a `SELECT` and survives both dialects without a bytea
#: round trip.
DIGEST_HEX = 32

_ZERO_FOLD = "0" * DIGEST_HEX


def _canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON for hashing and for the ``attrs`` column.

    Key-sorted and separator-compact, exactly as the file backend serialized
    the whole graph — so a node's bytes here are the bytes it had there, and
    the digest of an unchanged node does not depend on which backend wrote it.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:DIGEST_HEX]


def fold(current: str, *digests: str) -> str:
    """XOR ``digests`` into ``current``.

    Its own inverse, which is the whole reason the id can be maintained
    incrementally: folding a digest in twice is folding it out.
    """

    value = int(current or _ZERO_FOLD, 16)
    for item in digests:
        value ^= int(item, 16)
    return format(value, f"0{DIGEST_HEX}x")


def node_row(kb_id: str, node_id: str, attrs: dict[str, Any]) -> tuple:
    """One ``graph_nodes`` row, with its digest, from a node's attributes."""

    rest = {key: value for key, value in attrs.items() if key not in PROMOTED_NODE_KEYS}
    encoded = _canonical(rest)
    promoted = [_text(attrs.get(key)) for key in PROMOTED_NODE_KEYS]
    digest = _digest(node_id, *[value or "" for value in promoted], encoded)
    return (kb_id, node_id, *promoted, encoded, digest)


def node_attrs(row: Any) -> dict[str, Any]:
    """Rebuild a node's attribute dict from its row.

    The inverse of :func:`node_row`, and the *only* inverse: a caller that
    reads ``row["attrs"]`` directly gets a node missing its type and label.
    Promoted columns win over the JSON so a row written by an older schema
    cannot shadow them.
    """

    attrs = dict(json.loads(row["attrs"] or "{}"))
    for key in PROMOTED_NODE_KEYS:
        value = row[key]
        if value is not None:
            attrs[key] = value
    return attrs


def edge_row(kb_id: str, source: str, target: str, seq: int, attrs: dict[str, Any]) -> tuple:
    """One ``graph_edges`` row, with its digest."""

    rest = {key: value for key, value in attrs.items() if key not in PROMOTED_EDGE_KEYS}
    encoded = _canonical(rest)
    edge_type = _text(attrs.get("type")) or "related"
    source_id = _text(attrs.get("source_id"))
    digest = _digest(source, target, edge_type, str(seq), source_id or "", encoded)
    return (kb_id, source, target, edge_type, int(seq), source_id, encoded, digest)


def edge_attrs(row: Any) -> dict[str, Any]:
    """Rebuild an edge's attribute dict from its row."""

    attrs = dict(json.loads(row["attrs"] or "{}"))
    attrs["type"] = row["type"]
    if row["source_id"] is not None:
        attrs["source_id"] = row["source_id"]
    return attrs


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _batched(items: list, size: int = BATCH) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _pair_clauses(kb_id: str, pairs: Iterable[tuple[str, str]]) -> Iterator[tuple[str, list[Any]]]:
    """``(sql, params)`` fragments addressing an exact set of endpoint pairs.

    Grouped by source and capped twice — see :data:`PAIR_GROUP_BATCH`. Yields
    nothing for an empty set, which is what lets both callers skip the
    statement entirely rather than build ``WHERE ... AND ()``.

    **``kb_id`` is repeated inside every term, and that is the whole point.**
    Factoring it out to ``WHERE kb_id=? AND (… OR …)`` reads better and costs
    a full table scan: SQLite's multi-index-OR optimization needs each branch
    to be independently indexable, and ``source=?`` alone is not a prefix of
    ``(kb_id, source, target, type, seq)``. ``EXPLAIN QUERY PLAN`` says
    ``SEARCH … USING INDEX idx_graph_edges_target (kb_id=?)`` for the tidy
    form and ``MULTI-INDEX OR`` with exact primary-key lookups for this one.
    Measured: 300ms per incremental commit at 100k files versus 2ms, and the
    tidy form was O(total graph) — which would have made the row backend cost
    what the file backend cost, for the same reason, one layer down.
    """

    grouped: dict[str, list[str]] = {}
    for source, target in sorted(pairs):
        grouped.setdefault(source, []).append(target)
    groups: list[tuple[str, list[str]]] = []
    for source, targets in grouped.items():
        for chunk in _batched(targets, PAIR_TARGET_BATCH):
            groups.append((source, chunk))
    for batch in _batched(groups, PAIR_GROUP_BATCH):
        clause = " OR ".join(
            f"(kb_id=? AND source=? AND target IN ({','.join('?' for _ in targets)}))"
            for _source, targets in batch
        )
        params: list[Any] = []
        for source, targets in batch:
            params.extend((kb_id, source))
            params.extend(targets)
        yield clause, params


def _endpoint_clauses(kb_id: str, node_ids: list[str]) -> Iterator[tuple[str, list[Any]]]:
    """``(sql, params)`` fragments for every edge touching one of ``node_ids``.

    Two indexable branches per node rather than one ``OR`` between two ``IN``
    lists, for the reason :func:`_pair_clauses` spells out: the out-edge branch
    rides the primary key and the in-edge branch rides
    ``idx_graph_edges_target``, and neither can be reached with ``kb_id``
    hoisted out of the ``OR``.
    """

    for batch in _batched(node_ids, PAIR_GROUP_BATCH):
        placeholders = ",".join("?" for _ in batch)
        clause = (
            f"(kb_id=? AND source IN ({placeholders})) OR (kb_id=? AND target IN ({placeholders}))"
        )
        yield clause, [kb_id, *batch, kb_id, *batch]


class GraphRowStore:
    """Read and write the graph as rows in the state store.

    Holds no graph. Every method is a query, which is the property that lets an
    API replica answer a neighbourhood request without the residency the file
    backend required of it.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        dialect = getattr(state, "dialect", None)
        self._postgres = bool(dialect is not None and dialect.is_postgres)

    # --- writing ---------------------------------------------------------

    def apply_delta(
        self,
        kb_id: str,
        *,
        node_upserts: Iterable[tuple[str, dict[str, Any]]] = (),
        node_removals: Iterable[str] = (),
        edge_upserts: Iterable[tuple[str, str, int, dict[str, Any]]] = (),
        edge_removals: Iterable[tuple[str, str]] = (),
    ) -> dict[str, int]:
        """Write what changed, and only what changed.

        Removing a node removes the edges on **both** of its endpoints, which
        is what the in-memory graph's ``remove_nodes_from`` does and therefore
        what a persisted graph has to keep doing — a dangling edge would show
        up in a traversal as a neighbour with no attributes. That delete is
        why ``idx_graph_edges_target`` exists: without it, removing one node
        scans the whole edge table, which is the O(edges) cost this change is
        here to take off the sync path.

        Returns the counts, so a caller can log what a commit actually cost
        rather than what it thinks it cost.
        """

        nodes = list(node_upserts)
        removed_nodes = [node for node in node_removals]
        edges = list(edge_upserts)
        removed_edges = list(edge_removals)
        if not (nodes or removed_nodes or edges or removed_edges):
            return {"nodes": 0, "edges": 0, "removed_nodes": 0, "removed_edges": 0}

        node_rows = [node_row(kb_id, node_id, attrs) for node_id, attrs in nodes]
        edge_rows = [
            edge_row(kb_id, source, target, seq, attrs) for source, target, seq, attrs in edges
        ]
        touched_nodes = [node_id for node_id, _attrs in nodes] + removed_nodes
        # An endpoint pair whose edges are being replaced wholesale, plus every
        # pair either of whose endpoints is disappearing.
        touched_pairs = {(source, target) for source, target, _seq, _attrs in edges}
        touched_pairs |= set(removed_edges)

        # A knowledge base with no rows has nothing to fold out and nothing to
        # delete, so the first write of a graph — a fresh region, or the
        # one-shot import of a graph file — skips both. Not a heuristic: it is
        # exactly true, and it is what keeps the O(N) case from also paying
        # O(N) round trips proving there was nothing there. Probed with a
        # ``LIMIT 1``, never a ``COUNT(*)``: counting the whole table on every
        # commit is the shape that made the file backend O(total) in the first
        # place, and it measured 364ms per commit at 100k files before this.
        fresh = self._is_empty(kb_id)
        node_out, edge_out = (
            ({}, {})
            if fresh
            else self._digests_for(kb_id, touched_nodes, touched_pairs, removed_nodes)
        )

        with self.state.conn:
            if not fresh:
                self._delete_nodes(kb_id, touched_nodes)
                self._delete_edges(kb_id, touched_pairs)
                self._delete_edges_touching(kb_id, removed_nodes)
            self._insert_nodes(node_rows)
            self._insert_edges(edge_rows)
            self._fold(
                kb_id,
                node_out=list(node_out.values()),
                node_in=[row[-1] for row in node_rows],
                edge_out=list(edge_out.values()),
                edge_in=[row[-1] for row in edge_rows],
                node_change=len(node_rows) - len(node_out),
                edge_change=len(edge_rows) - len(edge_out),
            )
        return {
            "nodes": len(node_rows),
            "edges": len(edge_rows),
            "removed_nodes": len(removed_nodes),
            "removed_edges": max(0, len(edge_out) - len(edge_rows)),
        }

    def _is_empty(self, kb_id: str) -> bool:
        """Does this knowledge base hold any graph row at all? One row, not a count."""

        return not self.state.rows(
            "SELECT node_id FROM graph_nodes WHERE kb_id=? LIMIT 1", (kb_id,)
        )

    def _digests_for(
        self,
        kb_id: str,
        touched_nodes: list[str],
        touched_pairs: set[tuple[str, str]],
        removed_nodes: list[str],
    ) -> tuple[dict[Any, str], dict[Any, str]]:
        """Digests of the rows this delta is about to replace or delete.

        Read *before* the write, because folding a row out needs the digest it
        had. Doing it in the same transaction as the write would be tidier and
        is not possible: the rows are gone by then.

        Keyed by the row's own primary key rather than accumulated in a list,
        and that is load-bearing twice. XOR is an involution, so folding one
        row out **twice** silently folds it back in — and an edge can be
        reached by both queries below, once as a replaced pair and once as the
        casualty of a removed endpoint. Keying by identity also makes the
        length an exact count of rows leaving, which is how the row counts
        stay maintained without a ``COUNT(*)``.
        """

        node_out: dict[Any, str] = {}
        for batch in _batched(touched_nodes):
            placeholders = ",".join("?" for _ in batch)
            for row in self.state.rows(
                "SELECT node_id, digest FROM graph_nodes WHERE kb_id=? "
                f"AND node_id IN ({placeholders})",
                (kb_id, *batch),
            ):
                node_out[str(row["node_id"])] = str(row["digest"])
        edge_out: dict[Any, str] = {}
        columns = ", ".join(EDGE_KEY)
        for clause, group in _pair_clauses(kb_id, touched_pairs):
            for row in self.state.rows(
                f"SELECT {columns}, digest FROM graph_edges WHERE {clause}",
                tuple(group),
            ):
                edge_out[tuple(row[column] for column in EDGE_KEY)] = str(row["digest"])
        for clause, group in _endpoint_clauses(kb_id, removed_nodes):
            for row in self.state.rows(
                f"SELECT {columns}, digest FROM graph_edges WHERE {clause}",
                tuple(group),
            ):
                edge_out[tuple(row[column] for column in EDGE_KEY)] = str(row["digest"])
        return node_out, edge_out

    def _delete_nodes(self, kb_id: str, node_ids: list[str]) -> None:
        for batch in _batched(node_ids):
            placeholders = ",".join("?" for _ in batch)
            self.state.conn.execute(
                f"DELETE FROM graph_nodes WHERE kb_id=? AND node_id IN ({placeholders})",
                (kb_id, *batch),
            )

    def _delete_edges(self, kb_id: str, pairs: set[tuple[str, str]]) -> None:
        for clause, group in _pair_clauses(kb_id, pairs):
            self.state.conn.execute(f"DELETE FROM graph_edges WHERE {clause}", tuple(group))

    def _delete_edges_touching(self, kb_id: str, node_ids: list[str]) -> None:
        for clause, group in _endpoint_clauses(kb_id, node_ids):
            self.state.conn.execute(f"DELETE FROM graph_edges WHERE {clause}", tuple(group))

    def _insert_nodes(self, rows: list[tuple]) -> None:
        columns = "kb_id,node_id,type,label,source_id,relative_path,artifact_id,attrs,digest"
        placeholders = ",".join("?" * 9)
        for batch in _batched(rows):
            self.state.conn.executemany(
                f"INSERT INTO graph_nodes({columns}) VALUES({placeholders})", batch
            )

    def _insert_edges(self, rows: list[tuple]) -> None:
        columns = "kb_id,source,target,type,seq,source_id,attrs,digest"
        placeholders = ",".join("?" * 8)
        for batch in _batched(rows):
            self.state.conn.executemany(
                f"INSERT INTO graph_edges({columns}) VALUES({placeholders})", batch
            )

    def delete_source(self, kb_id: str, source_name: str) -> dict[str, int]:
        """Drop everything one source contributed, edges on both endpoints.

        The bulk case (``remove_source_content``, ``replace_source``), and the
        one place a whole-source scan is the right shape: it is indexed on
        ``source_id`` and it is what the caller asked for.
        """

        rows = self.state.rows(
            "SELECT node_id FROM graph_nodes WHERE kb_id=? AND source_id=?",
            (kb_id, source_name),
        )
        return self.apply_delta(kb_id, node_removals=[str(row["node_id"]) for row in rows])

    # --- publication -----------------------------------------------------

    def publish(self, kb_id: str) -> dict[str, Any]:
        """Name the current row set, and record it as the published generation.

        Content-addressed and clock-free, exactly as the file backend's
        ``generation_id`` was: an unchanged graph keeps its name across a
        re-publish, and two replicas holding the same rows compute the same id
        without talking to each other. ``published_at`` is recorded beside it
        and is deliberately *not* an input to the digest.
        """

        rows = self.state.rows(
            "SELECT node_fold, edge_fold FROM graph_generations WHERE kb_id=?", (kb_id,)
        )
        node_fold = str(rows[0]["node_fold"]) if rows else _ZERO_FOLD
        edge_fold = str(rows[0]["edge_fold"]) if rows else _ZERO_FOLD
        nodes, edges = self.counts(kb_id)
        identifier = _digest(node_fold, edge_fold, str(nodes), str(edges))[:16]
        published_at = datetime.now(UTC).isoformat()
        self.state.execute(
            "INSERT INTO graph_generations"
            "(kb_id,generation_id,published_at,nodes,edges,node_fold,edge_fold)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(kb_id) DO UPDATE SET"
            " generation_id=excluded.generation_id, published_at=excluded.published_at,"
            " nodes=excluded.nodes, edges=excluded.edges",
            (kb_id, identifier, published_at, nodes, edges, node_fold, edge_fold),
        )
        return {
            "generation_id": identifier,
            "published_at": published_at,
            "nodes": nodes,
            "edges": edges,
        }

    def published_generation(self, kb_id: str) -> dict[str, Any] | None:
        rows = self.state.rows(
            "SELECT generation_id, published_at, nodes, edges FROM graph_generations WHERE kb_id=?",
            (kb_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "generation_id": str(row["generation_id"]),
            "published_at": row["published_at"],
            "nodes": int(row["nodes"]),
            "edges": int(row["edges"]),
        }

    def _fold(
        self,
        kb_id: str,
        *,
        node_out: list[str],
        node_in: list[str],
        edge_out: list[str],
        edge_in: list[str],
        node_change: int,
        edge_change: int,
    ) -> None:
        """Update the persisted aggregates and counts. Caller holds the transaction.

        The counts are maintained here, from the exact number of rows this
        delta inserted and replaced, for the same reason the folds are: a
        ``COUNT(*)`` is a full scan, and the point of the backend is that a
        commit costs what the change costs. :meth:`recompute_folds` recounts
        the expensive way when something needs checking.
        """

        rows = self.state.rows(
            "SELECT nodes, edges, node_fold, edge_fold FROM graph_generations WHERE kb_id=?",
            (kb_id,),
        )
        node_fold = fold(str(rows[0]["node_fold"]) if rows else _ZERO_FOLD, *node_out, *node_in)
        edge_fold = fold(str(rows[0]["edge_fold"]) if rows else _ZERO_FOLD, *edge_out, *edge_in)
        node_count = max(0, (int(rows[0]["nodes"]) if rows else 0) + node_change)
        edge_count = max(0, (int(rows[0]["edges"]) if rows else 0) + edge_change)
        if rows:
            self.state.conn.execute(
                "UPDATE graph_generations SET node_fold=?, edge_fold=?, nodes=?, edges=?"
                " WHERE kb_id=?",
                (node_fold, edge_fold, node_count, edge_count, kb_id),
            )
            return
        self.state.conn.execute(
            "INSERT INTO graph_generations"
            "(kb_id,generation_id,published_at,nodes,edges,node_fold,edge_fold)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                kb_id,
                "",
                datetime.now(UTC).isoformat(),
                node_count,
                edge_count,
                node_fold,
                edge_fold,
            ),
        )

    def recompute_folds(self, kb_id: str) -> dict[str, str]:
        """Re-derive both aggregates with a full scan, and store them.

        The check on the incremental path. An aggregate maintained by deltas is
        only as good as every delta that ever ran, so ``sync --mode repair``
        and the tests recompute it the expensive way and compare — the same
        posture ``pheasant.evaluation.benchmark`` takes toward the capacity
        coefficients, and for the same reason: an unverified derived number
        drifts silently and is believed anyway.
        """

        nodes = _ZERO_FOLD
        for row in self._stream("graph_nodes", ("node_id",), kb_id):
            nodes = fold(nodes, str(row["digest"]))
        edges = _ZERO_FOLD
        for row in self._stream("graph_edges", EDGE_KEY, kb_id):
            edges = fold(edges, str(row["digest"]))
        node_count, edge_count = self.recount(kb_id)
        self.state.execute(
            "INSERT INTO graph_generations"
            "(kb_id,generation_id,published_at,nodes,edges,node_fold,edge_fold)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(kb_id) DO UPDATE SET"
            " node_fold=excluded.node_fold, edge_fold=excluded.edge_fold",
            (
                kb_id,
                "",
                datetime.now(UTC).isoformat(),
                node_count,
                edge_count,
                nodes,
                edges,
            ),
        )
        return {"node_fold": nodes, "edge_fold": edges}

    # --- reading ---------------------------------------------------------

    def counts(self, kb_id: str) -> tuple[int, int]:
        """Node and edge counts, from the maintained row.

        Read, never computed. ``COUNT(*)`` over the graph tables is a full
        scan, these are on the endpoints the UI polls and on every commit, and
        that is exactly the mistake the in-memory graph already fixed by
        maintaining ``_type_counts`` on write. :meth:`recount` is the honest
        scan for when the maintained number needs checking.
        """

        rows = self.state.rows("SELECT nodes, edges FROM graph_generations WHERE kb_id=?", (kb_id,))
        if rows:
            return int(rows[0]["nodes"]), int(rows[0]["edges"])
        return self.recount(kb_id)

    def recount(self, kb_id: str) -> tuple[int, int]:
        """The real counts, by scanning. For repair and for the tests."""

        nodes = self.state.rows("SELECT COUNT(*) AS n FROM graph_nodes WHERE kb_id=?", (kb_id,))
        edges = self.state.rows("SELECT COUNT(*) AS n FROM graph_edges WHERE kb_id=?", (kb_id,))
        return (int(nodes[0]["n"]) if nodes else 0, int(edges[0]["n"]) if edges else 0)

    def type_counts(self, kb_id: str) -> dict[str, int]:
        rows = self.state.rows(
            "SELECT type, COUNT(*) AS n FROM graph_nodes WHERE kb_id=? GROUP BY type",
            (kb_id,),
        )
        return {str(row["type"]): int(row["n"]) for row in rows if row["type"] is not None}

    def get_node(self, kb_id: str, node_id: str) -> dict[str, Any] | None:
        rows = self.state.rows(
            "SELECT * FROM graph_nodes WHERE kb_id=? AND node_id=?", (kb_id, node_id)
        )
        return node_attrs(rows[0]) if rows else None

    def get_nodes(self, kb_id: str, node_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Many nodes in one round trip — what a batched traversal hop needs."""

        found: dict[str, dict[str, Any]] = {}
        for batch in _batched(list(node_ids)):
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            for row in self.state.rows(
                f"SELECT * FROM graph_nodes WHERE kb_id=? AND node_id IN ({placeholders})",
                (kb_id, *batch),
            ):
                found[str(row["node_id"])] = node_attrs(row)
        return found

    def out_edges(
        self, kb_id: str, sources: Iterable[str]
    ) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
        """``{source: [(source, target, attrs)]}`` for a whole BFS frontier.

        One query per *level* rather than per node. The primary key already
        leads with ``(kb_id, source)``, so this is a range scan over the key
        and needs no index of its own.

        Ordered by ``(target, type, seq)`` and not by insertion. The in-memory
        graph enumerates neighbours in insertion order, which decides which
        ones survive a caller's ``max_nodes`` — an order the rows cannot
        reproduce and should not pretend to. A stable, stated ordering is the
        honest replacement; ``tests/test_graph_backends.py`` asserts the two
        backends return the same *set* and each is internally deterministic,
        rather than asserting an equality that is not true.
        """

        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        for batch in _batched(list(sources)):
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            for row in self.state.rows(
                f"SELECT * FROM graph_edges WHERE kb_id=? AND source IN ({placeholders})"
                " ORDER BY source, target, type, seq",
                (kb_id, *batch),
            ):
                grouped.setdefault(str(row["source"]), []).append(
                    (str(row["source"]), str(row["target"]), edge_attrs(row))
                )
        return grouped

    def iter_nodes(self, kb_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Every node, streamed. For exports and whole-graph passes."""

        for row in self._stream("graph_nodes", ("node_id",), kb_id):
            yield str(row["node_id"]), node_attrs(row)

    def iter_edges(self, kb_id: str) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        """Every edge, streamed and grouped by endpoint pair.

        Grouped to match the in-memory graph's ``iter_edges`` shape, so the
        exporter, the analytics extract and the relationship scan read from
        either backend with no branch. Ordered by the pair, which is what lets
        the grouping be streaming rather than a dict of the whole graph.
        """

        current: tuple[str, str] | None = None
        parallel: dict[int, dict] = {}
        for row in self._stream("graph_edges", EDGE_KEY, kb_id):
            pair = (str(row["source"]), str(row["target"]))
            if pair != current:
                if current is not None:
                    yield current, parallel
                current, parallel = pair, {}
            parallel[len(parallel)] = edge_attrs(row)
        if current is not None:
            yield current, parallel

    def candidate_edges(
        self,
        kb_id: str,
        tokens: list[str],
        source_name: str | None = None,
        limit: int = 2000,
    ) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        """Edges that could possibly score, instead of all of them.

        Relationship search scored every edge — O(edges), on the request path,
        with no index. ``_scan_edges`` matches on the edge's type, its
        endpoints' labels and its attribute values, so the narrowing here is
        the part a database can do: edge type, and endpoints whose node row
        matches. A token that matches nothing returns nothing, which is the
        common case and the one that used to cost the most.

        Deliberately a *superset* of what will score, never a subset: the
        scorer is unchanged and still decides. ``limit`` bounds the work rather
        than the answer — an edge beyond it is one the in-memory scan would
        also have had to reach past ``max_results`` to return.
        """

        if not tokens:
            return
        like = [f"%{token}%" for token in tokens]
        type_clause = " OR ".join("LOWER(e.type) LIKE ?" for _ in like)
        label_clause = " OR ".join("LOWER(n.label) LIKE ?" for _ in like)
        source_clause = " AND e.source_id=?" if source_name else ""
        params: list[Any] = [kb_id, *like]
        if source_name:
            params.append(source_name)
        params.extend([kb_id, *like, kb_id])
        if source_name:
            params.append(source_name)
        sql = (
            "SELECT * FROM graph_edges e WHERE e.kb_id=? AND"
            f" ({type_clause}){source_clause}"
            " UNION SELECT e.* FROM graph_edges e JOIN graph_nodes n"
            " ON n.kb_id=e.kb_id AND (n.node_id=e.source OR n.node_id=e.target)"
            f" WHERE n.kb_id=? AND ({label_clause}) AND e.kb_id=?{source_clause}"
            " ORDER BY source, target, type, seq LIMIT ?"
        )
        params.append(int(limit))
        current: tuple[str, str] | None = None
        parallel: dict[int, dict] = {}
        for row in self.state.rows(sql, tuple(params)):
            pair = (str(row["source"]), str(row["target"]))
            if pair != current:
                if current is not None:
                    yield current, parallel
                current, parallel = pair, {}
            parallel[len(parallel)] = edge_attrs(row)
        if current is not None:
            yield current, parallel

    #: Rows per page when scanning a whole table. Big enough that a 630k-node
    #: graph is 126 round trips rather than 630, small enough that a page is
    #: never the memory problem the paging exists to avoid.
    PAGE = 5_000

    def _stream(self, table: str, key: tuple[str, ...], kb_id: str) -> Iterator[Any]:
        """Every row of one graph table for one kb, in key order, a page at a time.

        Keyset pagination rather than a server-side cursor. ``StateStore.rows``
        is list-returning by contract — right for the bounded call sites it
        exists for, wrong for a scan of every node in the graph, which is the
        exact shape ``persistence/migrate.py`` streams ``chunks`` for. Keyset
        paging gets the same bounded memory through the ordinary read path, so
        it needs no new backend method and behaves identically on both
        dialects; ``OFFSET`` would not, because its cost grows with how far in
        the scan has already got.
        """

        columns = ", ".join(key)
        placeholders = ", ".join("?" for _ in key)
        after: tuple | None = None
        while True:
            if after is None:
                sql = f"SELECT * FROM {table} WHERE kb_id=? ORDER BY {columns} LIMIT ?"
                params: tuple = (kb_id, self.PAGE)
            else:
                sql = (
                    f"SELECT * FROM {table} WHERE kb_id=? AND ({columns}) > ({placeholders})"
                    f" ORDER BY {columns} LIMIT ?"
                )
                params = (kb_id, *after, self.PAGE)
            page = self.state.rows(sql, params)
            if not page:
                return
            yield from page
            last = page[-1]
            after = tuple(last[column] for column in key)
            if len(page) < self.PAGE:
                return
