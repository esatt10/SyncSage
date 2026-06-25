from __future__ import annotations

from typing import Any

from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.search.graph_search import search_graph
from syncsage.search.sqlite_store import SearchStore
from syncsage.search.vector_store import VectorSearcher

VALID_MODES = {"hybrid", "text", "graph", "vector"}


class HybridSearch:
    def __init__(self, store: SearchStore, vector: VectorSearcher | None = None):
        self.store = store
        # Optional semantic candidates (Synapse 21.4). None when
        # search.embeddings is disabled: "vector" contributes nothing and
        # every other mode behaves exactly as before.
        self.vector = vector

    def search_context(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        source_name: str | None = None,
        graph: SimpleMultiDiGraph | None = None,
    ) -> dict:
        mode = mode if mode in VALID_MODES else "hybrid"
        max_results = max(1, int(max_results or 10))

        text_results: list[dict[str, Any]] = []
        graph_results: list[dict[str, Any]] = []
        vector_results: list[dict[str, Any]] = []

        if mode in {"hybrid", "text"}:
            text_results = self.store.search(
                query, source_name=source_name, max_results=max_results
            )
        # Graph search needs the live graph; callers that don't supply one
        # (e.g. CLI/MCP search_context) transparently fall back to text search.
        if mode in {"hybrid", "graph"} and graph is not None:
            graph_results = search_graph(
                graph, query, max_results=max_results, source_name=source_name
            )
        if mode in {"hybrid", "vector"} and self.vector is not None:
            vector_results = self.vector.search(
                query, source_name=source_name, max_results=max_results
            )

        chunk_candidates = text_results + vector_results
        if text_results and vector_results:
            # Both engines contributed: order chunk candidates by their
            # normalized [0, 1] scores before merging with graph hits.
            chunk_candidates = sorted(
                chunk_candidates, key=lambda item: -float(item.get("score") or 0.0)
            )
        results = _merge(chunk_candidates, graph_results, max_results)
        return {
            "query": query,
            "knowledge_base": knowledge_base,
            "mode": mode,
            "results": results,
            "counts": {
                "text": len(text_results),
                "graph": len(graph_results),
                "vector": len(vector_results),
                "returned": len(results),
            },
        }


def _merge(
    text_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
    """Interleave text/vector and graph hits, deduplicating nodes by node_id.

    Text (chunk/FTS) and vector hits are favoured for files because they
    carry chunk previews and line ranges; graph hits add concepts, symbols,
    entities and relationships that the FTS index never surfaces. Chunk hits
    additionally dedupe on chunk_id so a vector hit never repeats an FTS hit.
    """

    deduped_chunks: list[dict[str, Any]] = []
    seen_chunks: set[str] = set()
    for item in text_results:
        chunk_id = item.get("chunk_id")
        if chunk_id and chunk_id in seen_chunks:
            continue
        if chunk_id:
            seen_chunks.add(chunk_id)
        deduped_chunks.append(item)
    text_results = deduped_chunks

    if not graph_results:
        results = text_results[:max_results]
        for rank, item in enumerate(results, start=1):
            item["rank"] = rank
        return results
    if not text_results:
        return graph_results[:max_results]

    merged: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()

    def _add(item: dict[str, Any]) -> None:
        node_id = item.get("node_id")
        if item.get("kind") != "relationship" and node_id and node_id in seen_nodes:
            return
        if node_id:
            seen_nodes.add(node_id)
        merged.append(item)

    combined = sorted(
        text_results + graph_results,
        key=lambda item: -float(item.get("score") or 0.0),
    )
    for item in combined:
        if len(merged) >= max_results:
            break
        _add(item)
    for rank, item in enumerate(merged, start=1):
        item["rank"] = rank
    return merged
