from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.search.graph_search import search_graph
from pheasant.search.sqlite_store import SearchStore, section_matches, section_needle
from pheasant.search.vector_store import VectorSearcher

logger = logging.getLogger(__name__)

VALID_MODES = {"hybrid", "text", "graph", "vector"}

#: Upper bound on a single query's result count. High enough that no
#: legitimate caller notices, low enough that a hostile one cannot ask the
#: region to load its whole index into memory.
MAX_RESULTS_CEILING = 500


class HybridSearch:
    def __init__(
        self,
        store: SearchStore,
        vector: VectorSearcher | None = None,
        node_index: Any = None,
        wasm_relationship_search: bool = False,
    ):
        self.store = store
        # Optional semantic candidates (Synapse 21.4). None when
        # search.embeddings is disabled: "vector" contributes nothing and
        # every other mode behaves exactly as before.
        self.vector = vector
        # Optional FTS index over graph nodes. None (or an empty index) makes
        # graph search scan the graph in memory, which is what it always did.
        self.node_index = node_index
        # Synapse Step 34.5b: opt-in WASM-accelerated relationship search
        # (search.wasm_relationship_search). Default off; falls back to pure
        # Python on any failure.
        self.wasm_relationship_search = wasm_relationship_search

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
        section: str | None = None,
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
        # A section filter narrows hard, so the arms that can only be filtered
        # after the fact (graph, which has no breadcrumb at all) need room to
        # find in-section hits — same over-fetch reasoning as the ACL pass.
        sectioned = bool(section_needle(section))
        fetch_n = max_results * 3 if (enforced or sectioned) else max_results

        text_results: list[dict[str, Any]] = []
        graph_results: list[dict[str, Any]] = []
        vector_results: list[dict[str, Any]] = []

        # The three retrievers are independent, and in hybrid mode their costs
        # are wildly different — a graph scan and a vector query that waits on
        # a remote embedding have no reason to queue behind each other. Running
        # them together makes hybrid cost about as much as its slowest arm
        # instead of the sum of all three. Each is I/O or read-only work, so
        # threads are enough (SQLite reads release the GIL; the embedder is a
        # network call).
        jobs: dict[str, Any] = {}
        if mode in {"hybrid", "text"}:
            jobs["text"] = lambda: self.store.search(
                query, source_name=source_name, max_results=fetch_n, section=section
            )
        # Graph search needs the live graph; callers that don't supply one
        # (e.g. CLI/MCP search_context) transparently fall back to text search.
        if mode in {"hybrid", "graph"} and graph is not None:
            jobs["graph"] = lambda: search_graph(
                graph,
                query,
                max_results=fetch_n,
                source_name=source_name,
                node_index=self.node_index,
                wasm_relationship_search=self.wasm_relationship_search,
            )
        if mode in {"hybrid", "vector"} and self.vector is not None:
            jobs["vector"] = lambda: self.vector.search(
                query, source_name=source_name, max_results=fetch_n
            )

        if len(jobs) == 1:
            # A single explicitly-requested arm (mode="text"/"graph"/"vector")
            # has nothing to fall back to, so a failure here still raises —
            # the caller asked for exactly this arm and silently returning
            # nothing would be a worse answer than an error.
            name, job = next(iter(jobs.items()))
            collected = {name: job()}
        else:
            # In "hybrid" mode the arms are independent and their failure
            # modes are not equivalent: text/graph read the local SQLite
            # store, but vector calls a remote embedding provider (network,
            # auth, quota). One arm's outage — e.g. a misconfigured or
            # expired embedding API key — must not take down text and graph
            # results that were already fetched successfully; it degrades
            # hybrid to whatever arms are actually healthy instead of
            # crashing the whole search (and, upstream, the assistant chat
            # request that depends on it).
            with ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
                futures = {name: pool.submit(job) for name, job in jobs.items()}
                collected = {}
                for name, future in futures.items():
                    try:
                        collected[name] = future.result()
                    except Exception:
                        logger.warning(
                            "hybrid search: %r arm failed, degrading", name, exc_info=True
                        )
                        collected[name] = []
        text_results = collected.get("text", [])
        graph_results = collected.get("graph", [])
        vector_results = collected.get("vector", [])

        if sectioned:
            # The text arm was already narrowed in SQL; the vector arm carries a
            # breadcrumb and is filtered here. Graph hits are dropped outright:
            # a symbol or entity node is not part of any document section, so it
            # cannot satisfy a claim about one (the same conservative call the
            # ACL pass makes for nodes with no artifact row).
            vector_results = [r for r in vector_results if section_matches(r.get("heading_path"), section)]
            graph_results = [r for r in graph_results if section_matches(r.get("heading_path"), section)]

        if enforced:
            from pheasant.security.acl import expand_principal, is_allowed

            identities = expand_principal(
                principal, principal_groups, getattr(security, "groups", None)
            )
            # Step 32.4 — union in IdP-synced groups, but only while the
            # mapping honors the staleness SLA (stale grants fail closed).
            if identities is not None and principal:
                from pheasant.security.idp import fresh_idp_groups

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

        results = _merge_rrf(text_results, vector_results, graph_results, max_results)
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


#: Reciprocal-rank-fusion constant. 60 is the value from the original RRF
#: paper and the usual default; it damps the top ranks just enough that one
#: arm's confident first place cannot alone decide the merge.
RRF_K = 60


def _merge_rrf(
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
    """Fuse the arms on **rank**, not on their scores.

    The three retrievers score on scales that are not comparable, and merging
    them by raw score silently reduced hybrid to whichever arm scored highest
    in absolute terms. Measured on a real corpus: text (BM25-derived) returned
    0.86-0.92, vector (cosine) 0.6679-0.6735, graph a flat 0.60 — so text won
    every position, every time, and the other two arms cost latency while
    contributing nothing to the ordering. Worse, each arm's internal spread
    was tiny (vector separated unrelated files by 0.006), so even within an
    arm the numbers barely ranked anything.

    Reciprocal rank fusion sidesteps calibration entirely: an item scores
    ``sum(1 / (RRF_K + rank))`` over the arms that returned it, so only each
    arm's own ordering matters and agreement between arms is what promotes a
    result. That is the property hybrid search was supposed to have.

    Ordering is deterministic: ties break on the fused score, then on the best
    rank any arm gave the item, then on node id.
    """

    def key_of(item: dict[str, Any]) -> str:
        return str(item.get("chunk_id") or item.get("node_id") or item.get("title") or "")

    fused: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    contributors: dict[str, set[str]] = {}

    for arm, results in (
        ("text", text_results),
        ("vector", vector_results),
        ("graph", graph_results),
    ):
        for rank, item in enumerate(results, start=1):
            key = key_of(item)
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            best_rank[key] = min(best_rank.get(key, rank), rank)
            contributors.setdefault(key, set()).add(arm)
            # Keep the richest record: chunk hits carry previews and line
            # ranges that graph hits do not, so a text/vector row wins the slot
            # even when the graph arm saw the item first.
            if key not in fused or (arm != "graph" and fused[key].get("kind") == "node"):
                fused[key] = item

    ordered = sorted(
        fused.values(),
        key=lambda item: (
            -scores[key_of(item)],
            best_rank[key_of(item)],
            key_of(item),
        ),
    )

    results: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for item in ordered:
        if len(results) >= max_results:
            break
        node_id = item.get("node_id")
        if item.get("kind") != "relationship" and node_id and node_id in seen_nodes:
            continue
        if node_id:
            seen_nodes.add(node_id)
        key = key_of(item)
        item["score"] = round(scores[key], 6)
        item["retrieved_by"] = "+".join(sorted(contributors[key]))
        results.append(item)

    for rank, item in enumerate(results, start=1):
        item["rank"] = rank
    return results


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
