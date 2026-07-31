"""Every SyncSage retrieval capability, as one typed object.

This is the toolbelt a question-answering workflow is handed. It is
deliberately **framework-agnostic** — no LangGraph, no LangChain, no LLM —
so the same surface backs the built-in single-shot workflow, the LangGraph
agent, and anything a user registers of their own. A workflow that wants a
different agent framework re-uses this rather than re-deriving how to reach
SyncSage's index.

It covers the full retrieval surface, not just "search":

* ``search`` — the hybrid self-search in every mode (``text`` / ``graph`` /
  ``vector`` / ``hybrid``), the same call the MCP ``search_context`` tool
  and ``POST /search`` make.
* ``neighbors`` / ``slice`` — typed graph traversal, so a workflow can walk
  from a hit into related material that lexical search never surfaces.
* ``content`` — full indexed text for a node, for when a preview is not
  enough to answer.
* ``facts`` — one-hop subject–predicate–object triples off the graph.
* ``capabilities`` — what this region can actually do right now (is a
  vector index built? which sources exist?), so a workflow can *plan*
  against reality instead of guessing.

Every method is read-only and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from syncsage.api.app import graph_neighbors as _graph_neighbors
from syncsage.api.app import graph_slice as _graph_slice

VALID_MODES = ("hybrid", "text", "graph", "vector")


@dataclass
class Passage:
    """One retrieved piece of evidence, normalized across search modes."""

    node_id: str | None
    chunk_id: str | None
    title: str
    relative_path: str | None
    source_id: str | None
    type: str | None
    score: float
    snippet: str
    mode: str
    raw: dict = field(default_factory=dict, repr=False)

    def key(self) -> str:
        return str(self.chunk_id or self.node_id or self.title)


@dataclass
class RetrievalCapabilities:
    """What this knowledge base can answer with, right now."""

    knowledge_base: str
    sources: list[str]
    modes: list[str]
    vector_enabled: bool
    vector_count: int
    chunk_count: int
    artifact_count: int
    node_counts: dict[str, int]

    def as_prompt_context(self) -> str:
        """A compact description a planner LLM can reason over."""
        lines = [
            f"Knowledge base: {self.knowledge_base}",
            f"Sources: {', '.join(self.sources) if self.sources else '(none)'}",
            f"Indexed files: {self.artifact_count}; passages: {self.chunk_count}",
            f"Search modes available: {', '.join(self.modes)}",
        ]
        if self.vector_enabled:
            lines.append(f"Semantic (vector) index: {self.vector_count} vectors built")
        else:
            lines.append("Semantic (vector) index: not enabled — lexical + graph only")
        return "\n".join(lines)


class SyncSageRetriever:
    """Read-only access to a knowledge base's full retrieval surface."""

    def __init__(
        self,
        *,
        search: Any,
        knowledge_base: str,
        graph: Any = None,
        state: Any = None,
        config: Any = None,
    ) -> None:
        self.search_engine = search
        self.knowledge_base = knowledge_base
        self.graph = graph
        self.state = state
        self.config = config
        # Per-request memo: an agent loop re-issues overlapping queries, and
        # paying twice for the identical (query, mode, limit, source) tuple is
        # pure waste — SyncSage's index does not change mid-answer.
        self._cache: dict[tuple, list[Passage]] = {}

    # ---------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        limit: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> list[Passage]:
        """Run the hybrid self-search and normalize the hits."""
        query = (query or "").strip()
        if not query:
            return []
        mode = mode if mode in VALID_MODES else "hybrid"
        cache_key = (query, mode, limit, source_name, principal)
        if cache_key in self._cache:
            return self._cache[cache_key]

        payload = self.search_engine.search_context(
            self.knowledge_base,
            query,
            mode,
            limit,
            source_name,
            graph=self.graph,
            principal=principal,
            principal_groups=principal_groups,
            security=getattr(self.config, "security", None),
        )
        passages = [self._passage(item, mode) for item in payload.get("results", [])]
        self._cache[cache_key] = passages
        return passages

    def multi_search(
        self,
        queries: list[str],
        *,
        modes: list[str] | None = None,
        limit: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> list[Passage]:
        """Fan out over queries × modes and merge, de-duplicated.

        Ordering is deterministic: results keep their best score, ties break
        on first appearance, so the same plan over an unchanged index
        produces the same evidence in the same order.
        """
        modes = [m for m in (modes or ["hybrid"]) if m in VALID_MODES] or ["hybrid"]
        merged: dict[str, Passage] = {}
        for query in queries:
            for mode in modes:
                for passage in self.search(
                    query,
                    mode=mode,
                    limit=limit,
                    source_name=source_name,
                    principal=principal,
                    principal_groups=principal_groups,
                ):
                    existing = merged.get(passage.key())
                    if existing is None:
                        merged[passage.key()] = passage
                        continue
                    # Found more than one way — that is a relevance signal, and
                    # it holds whichever mode scored higher (often they tie,
                    # since the modes share one corpus). Record the union of
                    # modes, keep the strongest score.
                    modes_seen = _merge_modes(existing.mode, passage.mode)
                    winner = passage if passage.score > existing.score else existing
                    winner.mode = modes_seen
                    merged[passage.key()] = winner
        # Sort by score, then by key so ties are stable across runs.
        return sorted(merged.values(), key=lambda p: (-p.score, p.key()))

    def _passage(self, item: dict, mode: str) -> Passage:
        chunks = item.get("chunks") or []
        snippet = ""
        for chunk in chunks:
            text = (chunk.get("text_preview") or "").strip()
            if text:
                snippet = text
                break
        if not snippet:
            snippet = str(item.get("summary") or item.get("label") or "").strip()
        provenance = item.get("provenance") or {}
        return Passage(
            node_id=item.get("node_id"),
            chunk_id=item.get("chunk_id"),
            title=str(
                item.get("title")
                or item.get("relative_path")
                or item.get("label")
                or item.get("node_id")
                or "untitled"
            ),
            relative_path=item.get("relative_path"),
            source_id=item.get("source_id") or provenance.get("source_id"),
            type=item.get("type"),
            score=float(item.get("score") or 0.0),
            snippet=snippet[:900],
            mode=mode,
            raw=item,
        )

    # ----------------------------------------------------------------- graph

    def neighbors(
        self, node_id: str, depth: int = 1, edge_types: list[str] | None = None
    ) -> list[dict]:
        """Breadth-first neighbours of a node (same shape as the MCP tool)."""
        if self.graph is None:
            return []
        return _graph_neighbors(self.graph, node_id, depth, edge_types).get("neighbors", [])

    def slice(self, node_id: str, depth: int = 1, limit: int = 40) -> dict:
        """Connected sub-graph around a node."""
        if self.graph is None:
            return {"node_id": node_id, "depth": depth, "nodes": [], "links": []}
        return _graph_slice(self.graph, node_id, depth, None, limit)

    def expand(
        self, passages: list[Passage], *, depth: int = 1, per_node: int = 4
    ) -> list[Passage]:
        """Follow the graph out of each passage into related documents.

        This is the capability a purely lexical RAG loop does not have: a
        question whose answer lives in a document that shares no vocabulary
        with the query is still reachable through a shared concept or an
        import/call edge. Returned passages carry ``mode="graph-expand"`` so
        a caller can tell derived evidence from direct hits.
        """
        if self.graph is None:
            return []
        seen = {p.node_id for p in passages if p.node_id}
        found: list[Passage] = []
        for passage in passages:
            if not passage.node_id:
                continue
            added = 0
            for entry in self.neighbors(passage.node_id, depth):
                node = entry.get("node") or {}
                node_id = str(entry.get("node_id") or "")
                if not node_id or node_id in seen:
                    continue
                # Only documents carry answerable prose; concept/chunk nodes
                # are navigation, not evidence.
                if node.get("type") not in ("file", "document", "markdown_note"):
                    continue
                seen.add(node_id)
                found.append(
                    Passage(
                        node_id=node_id,
                        chunk_id=None,
                        title=str(node.get("label") or node_id),
                        relative_path=node.get("relative_path"),
                        source_id=node.get("source_id"),
                        type=node.get("type"),
                        # Derived evidence ranks below any direct hit.
                        score=max(0.0, passage.score * 0.4),
                        snippet=str(node.get("summary") or "")[:600],
                        mode="graph-expand",
                        raw=node,
                    )
                )
                added += 1
                if added >= per_node:
                    break
        return found

    def facts(self, node_ids: list[str], limit: int = 12) -> list[dict]:
        """One-hop subject–predicate–object triples around these nodes."""
        from syncsage.assistant.chat import collect_facts

        return collect_facts(self.graph, node_ids, limit)

    # --------------------------------------------------------------- content

    def content(self, node_id: str, max_chars: int = 6000) -> str | None:
        """Full indexed text for a node, when a preview is not enough."""
        if self.state is None:
            return None
        rows = self.state.rows("SELECT text FROM chunks WHERE id=? LIMIT 1", (node_id,))
        if rows:
            return str(rows[0]["text"])[:max_chars]
        rows = self.state.rows(
            "SELECT GROUP_CONCAT(text, '\n\n') AS content FROM "
            "(SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index)",
            (node_id,),
        )
        content = rows[0]["content"] if rows else None
        return str(content)[:max_chars] if content else None

    # ---------------------------------------------------------- capabilities

    def capabilities(self) -> RetrievalCapabilities:
        """What this knowledge base can answer with, right now."""
        modes = ["hybrid", "text"]
        if self.graph is not None:
            modes.append("graph")
        vector_enabled = getattr(self.search_engine, "vector", None) is not None
        vector_count = 0
        if vector_enabled:
            modes.append("vector")
            try:
                vector_count = int(self.search_engine.vector.store.count())
            except Exception:  # a missing/corrupt store must not break planning
                vector_count = 0

        sources: list[str] = []
        chunk_count = artifact_count = 0
        if self.state is not None:
            try:
                sources = [
                    str(row["source_id"])
                    for row in self.state.rows(
                        "SELECT DISTINCT source_id FROM artifacts ORDER BY source_id"
                    )
                ]
                chunk_count = int(self.state.rows("SELECT COUNT(*) AS n FROM chunks")[0]["n"])
                artifact_count = int(self.state.rows("SELECT COUNT(*) AS n FROM artifacts")[0]["n"])
            except Exception:
                pass

        node_counts: dict[str, int] = {}
        if self.graph is not None:
            for _node_id, attrs in self.graph.nodes(data=True):
                node_type = str(attrs.get("type") or "unknown")
                node_counts[node_type] = node_counts.get(node_type, 0) + 1

        return RetrievalCapabilities(
            knowledge_base=self.knowledge_base,
            sources=sources,
            modes=modes,
            vector_enabled=vector_enabled,
            vector_count=vector_count,
            chunk_count=chunk_count,
            artifact_count=artifact_count,
            node_counts=node_counts,
        )


def _merge_modes(*modes: str) -> str:
    """Union of retrieval modes, de-duplicated and canonically ordered.

    Sorted rather than discovery-ordered on purpose: this string is a label
    ("retrieved by hybrid+vector"), and two passages found by the same pair
    of modes must read the same way regardless of which query hit first.
    """
    seen = {mode for group in modes for mode in str(group).split("+") if mode}
    return "+".join(sorted(seen))
