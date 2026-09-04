"""Retrieval, once — the operation that had already diverged furthest.

`search` and `relevant_files` existed on both surfaces. Each carried its own
over-fetch arithmetic (fixed separately: see `RankingParameters.overfetch`),
its own criteria assembly, and its own idea of which optional behaviours
applied. What that produced, measured before this module existed:

* **`relevant_files` ignored the memory policy over MCP.** The HTTP route
  passed ``memory=``; the tool did not. So an agent — the consumer memory
  exists for — could be handed a record the region *knew* had been superseded,
  which is precisely the stale-fact failure the memory plane was built to
  prevent.
* **`relevant_files` did not deduplicate over MCP.** HTTP returned one entry
  per file; the tool returned one per chunk, so an agent asking for eight
  files could receive eight chunks of two.
* **`relevant_files` ignored `section` over MCP.**
* **Search metrics were HTTP-only.** ``pheasant_search_total`` and
  ``pheasant_search_duration_seconds`` were incremented in the HTTP route, so
  every search an agent ran through MCP was invisible to the counters an
  operator sizes the region with — and to the capacity model that reads them.

None of those was a decision. Each is a line that landed on the surface whose
bug report arrived first.

Observation stays in the adapters on purpose: the ledger event carries the
HTTP request or the MCP session that opened it, which is transport context by
definition. What the adapters record — the query, the payload, the criteria —
is what this module returns, so the *content* is still decided once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pheasant.search.criteria import apply_retrieval_criteria, criteria_active, criteria_dict
from pheasant.services import ServiceContext
from pheasant.telemetry import metrics


@dataclass(frozen=True)
class SearchRequest:
    """One retrieval call, in the vocabulary both surfaces already speak.

    Typed rather than a bag of keyword arguments because this is the boundary
    the review named: an operation whose payload has no declared shape cannot
    be moved, and cannot be checked for agreement between two callers.
    """

    query: str
    knowledge_base: str | None = None
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None
    section: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    memory: Any = None
    #: Post-filters applied after the merge. Every one of these is a reason to
    #: over-fetch, which is why they travel together.
    exclude_sources: list[str] | None = None
    node_types: list[str] | None = None
    min_score: float | None = None
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    #: Ask the arms to report what each stage did. The tuning plane's whole
    #: diagnosis is this block; see `search.explain`.
    explain: bool = False

    @property
    def filtering(self) -> bool:
        """Whether a post-filter will drop rows, and the arms must over-fetch."""

        return criteria_active(
            self.exclude_sources,
            self.node_types,
            self.min_score,
            self.source_types,
            self.exclude_source_types,
        )

    def criteria_block(self) -> dict[str, Any]:
        return criteria_dict(
            self.source_name,
            self.exclude_sources,
            self.node_types,
            self.min_score,
            self.memory,
            self.source_types,
            self.exclude_source_types,
        )


@dataclass(frozen=True)
class FilesRequest:
    """`relevant_files`: the same retrieval, projected to files."""

    task: str
    knowledge_base: str | None = None
    max_files: int = 8
    source_name: str | None = None
    section: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    memory: Any = None


def search(context: ServiceContext, request: SearchRequest) -> dict[str, Any]:
    """Hybrid retrieval with criteria, over-fetch, metrics and provenance.

    The order is load-bearing. Over-fetch is decided *before* the arms run,
    from the ranking parameters rather than a literal; the criteria filter runs
    *after* the merge and truncates to what the caller asked for; the metric is
    recorded around retrieval only, so criteria bookkeeping does not read as
    retrieval latency.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    ranking = context.searcher.ranking_parameters()
    fetch = ranking.overfetch(request.max_results, filtering=request.filtering)

    started = time.perf_counter()
    try:
        payload = context.searcher.search_context(
            kb_id,
            request.query,
            request.mode,
            fetch,
            request.source_name,
            graph=context.graph,
            principal=request.principal,
            principal_groups=request.principal_groups,
            security=context.config.security,
            section=request.section,
            memory=request.memory,
            explain=request.explain,
        )
    except Exception:
        metrics.REGISTRY.inc("pheasant_search_total", mode=request.mode, outcome="error")
        raise
    finally:
        # Timed around retrieval only. Wrapping the post-filter too would fold
        # criteria bookkeeping into what reads as retrieval latency.
        metrics.REGISTRY.observe(
            "pheasant_search_duration_seconds", time.perf_counter() - started, mode=request.mode
        )
    metrics.REGISTRY.inc("pheasant_search_total", mode=request.mode, outcome="ok")

    if request.filtering:
        payload = dict(payload)
        payload["results"] = apply_retrieval_criteria(
            payload.get("results") or [],
            exclude_sources=request.exclude_sources,
            node_types=request.node_types,
            min_score=request.min_score,
            source_types=request.source_types,
            exclude_source_types=request.exclude_source_types,
        )[: request.max_results]
        payload["criteria"] = request.criteria_block()
    else:
        payload = dict(payload)

    # Which graph answered. A retrieval diagnosis that cannot name the
    # generation cannot tell "the document is not indexed" from "this replica
    # has not picked up the index that has it" — and those call for opposite
    # responses.
    payload["graph_generation"] = getattr(context.engine, "loaded_graph_generation", None)
    return payload


def relevant_files(context: ServiceContext, request: FilesRequest) -> dict[str, Any]:
    """The same retrieval as :func:`search`, projected to distinct files.

    No ``graph=``: this answers with *files*, and graph nodes (concepts,
    symbols) carry no ``relative_path``, so admitting them would crowd the file
    hits out of the merge and return an empty list.

    It runs under the same ACL enforcement and the same memory policy as
    `search`, which is the correction this module exists to make permanent:
    dropping either one silently served unfiltered — and in the memory case,
    *corrected* — content to whichever surface forgot it.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    payload = context.searcher.search_context(
        kb_id,
        request.task,
        "hybrid",
        request.max_files,
        request.source_name,
        principal=request.principal,
        principal_groups=request.principal_groups,
        security=context.config.security,
        section=request.section,
        memory=request.memory,
    )
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    for result in payload.get("results") or []:
        relative_path = result.get("relative_path")
        if relative_path and relative_path not in seen:
            seen.add(relative_path)
            files.append(result)
    return {"files": files}
