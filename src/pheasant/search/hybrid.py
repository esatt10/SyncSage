from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.memory.policy import (
    PREFER_SHARE,
    MemoryPolicy,
    admits,
    describe,
    load_memory_index,
    may_filter,
    resolve,
    utc_now_iso,
)
from pheasant.search.criteria import source_type_map, stamp_source_types
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
        steering_enabled: bool = False,
        default_memory_policy: str = "auto",
        usage_tracking: bool = False,
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
        # Step 33.8: let memory rules steer ranking (memory.steering_enabled).
        # Default off, so a region that has not asked for it ranks exactly as
        # it did before.
        self.steering_enabled = steering_enabled
        # memory.default_policy — what a caller that says nothing gets. A
        # per-call `memory` argument always wins, so this only fills the gap.
        self.default_memory_policy = default_memory_policy
        # memory.usage_tracking — count which memories retrieval returns, so
        # salience can reflect use. Off by default: it is a write on the read
        # path and recording what someone looks up is an operator's choice.
        self.usage_tracking = usage_tracking

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
        memory: Any = None,
        steering_kinds: tuple[str, ...] | None = None,
        extra_steering_records: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Run one query across the arms and fuse them.

        ``steering_kinds`` narrows which memory rule kinds may fire. ``None``
        -- every production caller -- means "whatever ``steering_enabled``
        says", so this behaves exactly as it did before the argument existed.
        The evaluation plane passes an explicit subset to build a paired
        ablation: measuring what *alias* rules contribute needs a run in which
        preference and exclusion rules do not fire, and one in which they do,
        differing in nothing else.

        ``extra_steering_records`` adds rule records that are **not in the
        store** -- proposed rules under shadow validation. They go through the
        same `parse_rule`/`admits` path as stored ones, so a shadow run
        measures the rule retrieval would really apply rather than a
        re-implementation of it, and nothing is written anywhere: a candidate
        cannot reach production ranking by being evaluated.
        """
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
        # Step 33.6 — agent-memory policy. The index is loaded once and shared:
        # the text arm filters in SQL (recall), the vector and graph arms filter
        # against the same rows below, and every arm's hits are annotated from
        # it. An empty index means the region has no memory records, and then
        # none of this does anything at all.
        policy = MemoryPolicy.parse(memory if memory is not None else self.default_memory_policy)
        memory_index = load_memory_index(getattr(self.store, "state", None))
        memory_now = utc_now_iso() if memory_index else None
        memory_filtered = may_filter(policy, memory_index, now=memory_now or "")
        # Step 33.8 — rules from `alias`/`preference`/`exclusion` records, put
        # through the same `admits` predicate as retrieval so a corrected or
        # out-of-scope record steers nothing. Empty unless steering is on.
        steering = None
        steering_on = self.steering_enabled if steering_kinds is None else bool(steering_kinds)
        if steering_on and (memory_index or extra_steering_records):
            from pheasant.memory.steering import load_steering, load_steering_records

            records = load_steering_records(self.store.state)
            records.extend(extra_steering_records or [])
            steering = (
                load_steering(
                    records,
                    policy,
                    now=memory_now or utc_now_iso(),
                    enabled=True,
                    kinds=steering_kinds,
                )
                or None
            )
        # A section filter narrows hard, so the arms that can only be filtered
        # after the fact (graph, which has no breadcrumb at all) need room to
        # find in-section hits — same over-fetch reasoning as the ACL pass, and
        # the same again for a memory policy, where `only` narrows to a handful
        # of records and `off` can drop a lot.
        sectioned = bool(section_needle(section))
        fetch_n = max_results * 3 if (enforced or sectioned or memory_filtered) else max_results

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
                query,
                source_name=source_name,
                max_results=fetch_n,
                section=section,
                memory_policy=policy if memory_index else None,
                memory_now=memory_now,
                steering=steering,
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
            vector_results = [
                r for r in vector_results if section_matches(r.get("heading_path"), section)
            ]
            graph_results = [
                r for r in graph_results if section_matches(r.get("heading_path"), section)
            ]

        if memory_index:
            # The text arm was already narrowed in SQL. These two carry no
            # memory columns of their own, so they are filtered against the
            # same rows through the same predicate — one rule, two encodings,
            # and `tests/test_memory_policy.py` pins them together.
            def memory_admits(item: dict[str, Any]) -> bool:
                return admits(policy, resolve(item, memory_index), now=memory_now)

            vector_results = [r for r in vector_results if memory_admits(r)]
            graph_results = [r for r in graph_results if memory_admits(r)]

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

        if memory_index and policy.mode == "prefer":
            text_results, vector_results, graph_results = _prefer_memory(
                text_results, vector_results, graph_results, memory_index, max_results
            )

        # Record which *kind* of source each hit came from, before the merge so
        # every arm is covered and before any caller-side criteria run. One
        # lookup per search, shared by all three arms.
        source_types = source_type_map(getattr(self.store, "state", None))
        for group in (text_results, vector_results, graph_results):
            stamp_source_types(group, source_types)

        results = _merge_rrf(text_results, vector_results, graph_results, max_results)
        if memory_index:
            _annotate_memory(results, memory_index, policy)
            if self.usage_tracking:
                # Step 33.9 — count what retrieval actually returned. Last,
                # after truncation, so a record is credited for being *served*
                # rather than merely considered. Best-effort by construction:
                # `record_memory_use` swallows its own failures, because a
                # ranking signal must never cost a query.
                self.store.state.record_memory_use(
                    sorted(
                        {
                            str(item["memory"]["record_id"])
                            for item in results
                            if item.get("memory") and item["memory"].get("record_id")
                        }
                    ),
                    memory_now,
                )
        payload = {
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
        # Reported only when the region has memory, so a corpus without it
        # returns the exact payload it did before — the same "add the key only
        # when it says something" rule `heading_path` follows.
        if memory_index:
            payload["memory_policy"] = policy.as_dict()
        if steering:
            # Reported so an agent can see *why* it got what it got — a rule
            # that silently re-orders results is the thing to avoid.
            payload["memory_steering"] = steering.describe()
        return payload


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


def _annotate_memory(
    results: list[dict[str, Any]],
    memory_index: dict[str, dict[str, Any]],
    policy: Any,
) -> None:
    """Mark which hits are remembered assertions rather than corpus content.

    Added only to hits that *are* memory records, so a corpus result keeps the
    exact shape it had before Step 33.6 — the same rule `heading_path` follows.

    This is not decoration. A caller that cannot tell a remembered assertion
    from a document has no way to weigh it differently, and the answering
    prompt and the UI both need the distinction to be explicit rather than
    inferred from a path that happens to start with a scope directory.
    """
    for item in results:
        record = resolve(item, memory_index)
        if record is None:
            continue
        item["memory"] = describe(record)
        provenance = item.get("provenance")
        if isinstance(provenance, dict):
            provenance["memory"] = True
    _ = policy


def _prefer_memory(
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    memory_index: dict[str, dict[str, Any]],
    max_results: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Guarantee memory a share of the slots under `mode="prefer"`.

    Fusion ranks memory against a whole corpus, so on a large index a genuinely
    relevant memory can lose every slot to documents that merely match more
    words. `prefer` reserves up to half the results for memory hits by moving
    them to the front of their own arm, which is enough for RRF to keep them:
    their rank within the arm is what the fusion actually reads.

    Deliberately a *reordering*, not a score change. Inventing a boost would
    make the same query answer differently depending on a flag, in a way no
    caller could predict; promoting within an arm is bounded, explainable and
    leaves every other hit's relative order intact.
    """
    reserved = max(1, int(max_results * PREFER_SHARE))

    def promote(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remembered = [r for r in results if resolve(r, memory_index) is not None]
        if not remembered:
            return results
        rest = [r for r in results if resolve(r, memory_index) is None]
        return remembered[:reserved] + rest + remembered[reserved:]

    return promote(text_results), promote(vector_results), promote(graph_results)
