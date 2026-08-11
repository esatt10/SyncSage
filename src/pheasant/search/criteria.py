"""Per-call retrieval criteria, shared by every protocol (Step 33.6).

These filters lived inside `mcp_server/tools.py`, which meant an MCP caller
could scope a search away from a noisy source, floor the score or keep only
certain node types — and an HTTP caller could not, because `POST /search` went
straight to the searcher and never saw them. Same region, same index, two
different retrieval surfaces depending on which protocol you happened to hold.

Moving them here is what lets `/search` and `search_context` call one
implementation. `mcp_server.tools` re-exports `apply_retrieval_criteria` so
existing importers keep working.
"""

from __future__ import annotations

from typing import Any


def apply_retrieval_criteria(
    results: list[dict],
    *,
    exclude_sources: list[str] | None = None,
    node_types: list[str] | None = None,
    min_score: float | None = None,
) -> list[dict]:
    """Filter search hits by caller-supplied criteria. Order is preserved.

    Ranking is not recomputed: these are *post-filters* over an already-ranked
    list, so a criterion can only ever remove rows. Re-scoring here would make
    the same query answer differently depending on which filters were passed,
    which is exactly the surprise a retrieval-tuning surface must not have.
    """
    excluded = {str(name) for name in (exclude_sources or [])}
    wanted_types = {str(name) for name in (node_types or [])}
    kept: list[dict] = []
    for item in results:
        if excluded and source_of(item) in excluded:
            continue
        if wanted_types and str(item.get("type") or "") not in wanted_types:
            continue
        if min_score is not None:
            try:
                if float(item.get("score") or 0.0) < min_score:
                    continue
            except (TypeError, ValueError):
                continue
        kept.append(item)
    return kept


def source_of(item: dict) -> str:
    """The source a search hit came from.

    Hits do **not** carry a top-level ``source_id`` — it lives under
    ``provenance``, alongside the path. Reading the wrong key here meant
    ``exclude_sources`` matched nothing and silently returned unfiltered
    results, which is worse than refusing the filter outright. Both shapes are
    accepted so a caller passing already-flattened rows still works.
    """
    provenance = item.get("provenance")
    if isinstance(provenance, dict) and provenance.get("source_id"):
        return str(provenance["source_id"])
    return str(item.get("source_id") or item.get("source") or "")


def criteria_active(
    exclude_sources: list[str] | None,
    node_types: list[str] | None,
    min_score: float | None,
) -> bool:
    """Whether any post-filter will run, and therefore whether to over-fetch.

    Over-fetching matters: without it `max_results` quietly degrades from "give
    me this many" to "look at this many and return whatever survives".
    """
    return bool(exclude_sources or node_types or min_score is not None)


def criteria_dict(
    source_name: str | None,
    exclude_sources: list[str] | None,
    node_types: list[str] | None,
    min_score: float | None,
    memory: Any = None,
) -> dict[str, Any]:
    """The echo of what was applied, reported back on a filtered payload."""
    return {
        "source_name": source_name,
        "exclude_sources": exclude_sources,
        "node_types": node_types,
        "min_score": min_score,
        "memory": memory,
    }
