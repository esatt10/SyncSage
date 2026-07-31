from __future__ import annotations

from typing import Any

from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.search.graph_search import search_graph
from syncsage.search.sqlite_store import SearchStore
from syncsage.search.vector_store import VectorSearcher

VALID_MODES = {"hybrid", "text", "graph", "vector"}

#: Upper bound on a single query's result count. High enough that no
#: legitimate caller notices, low enough that a hostile one cannot ask the
#: region to load its whole index into memory.
MAX_RESULTS_CEILING = 500


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
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        security: Any = None,
    ) -> dict:
        mode = mode if mode in VALID_MODES else "hybrid"
        # Clamped, not just floored: `max_results` arrives straight off an
        # unauthenticated HTTP body, and an over-fetching ACL pass multiplies
        # it. Without a ceiling a single request can ask the region to
        # materialize the entire index in memory.
        max_results = min(max(1, int(max_results or 10)), MAX_RESULTS_CEILING)

        # Step 32.2 — principal-aware retrieval, opt-in via
        # security.acl_enforced. Candidates are over-fetched, filtered
        # against artifact ACLs *before* the merge/return, then truncated.
        enforced = bool(security is not None and getattr(security, "acl_enforced", False))
        fetch_n = max_results * 3 if enforced else max_results

        text_results: list[dict[str, Any]] = []
        graph_results: list[dict[str, Any]] = []
        vector_results: list[dict[str, Any]] = []

        if mode in {"hybrid", "text"}:
            text_results = self.store.search(query, source_name=source_name, max_results=fetch_n)
        # Graph search needs the live graph; callers that don't supply one
        # (e.g. CLI/MCP search_context) transparently fall back to text search.
        if mode in {"hybrid", "graph"} and graph is not None:
            graph_results = search_graph(graph, query, max_results=fetch_n, source_name=source_name)
        if mode in {"hybrid", "vector"} and self.vector is not None:
            vector_results = self.vector.search(query, source_name=source_name, max_results=fetch_n)

        if enforced:
            from syncsage.security.acl import expand_principal, is_allowed

            identities = expand_principal(
                principal, principal_groups, getattr(security, "groups", None)
            )
            # Step 32.4 — union in IdP-synced groups, but only while the
            # mapping honors the staleness SLA (stale grants fail closed).
            if identities is not None and principal:
                from syncsage.security.idp import fresh_idp_groups

                identities |= fresh_idp_groups(
                    self.store.state, principal, getattr(security, "idp", None)
                )
            default_public = getattr(security, "default_visibility", "public") != "private"
            candidate_ids = [
                str(item.get("node_id"))
                for group in (text_results, vector_results, graph_results)
                for item in group
                if item.get("node_id")
            ]
            acls = self.store.state.artifact_acls(candidate_ids)

            def visible(item: dict[str, Any]) -> bool:
                node_id = str(item.get("node_id") or "")
                if node_id not in acls:
                    # Not resolvable to an artifact row (concept/symbol graph
                    # nodes): conservative deny under enforcement.
                    return False
                return is_allowed(acls[node_id], identities, default_public=default_public)

            text_results = [r for r in text_results if visible(r)]
            vector_results = [r for r in vector_results if visible(r)]
            graph_results = [r for r in graph_results if visible(r)]

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
