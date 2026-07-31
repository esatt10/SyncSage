"""Retrieval → grounding → answer, with graph facts alongside the prose.

The pipeline is deliberately thin, and every step is inspectable:

1. run the ordinary hybrid self-search for the question;
2. turn the hits into numbered passages plus citation records;
3. read *facts* straight off the knowledge graph — one hop out from each
   cited node into the concept/entity/symbol layer, rendered as
   subject–predicate–object triples;
4. ask a chat model to answer using only those passages, citing ``[n]``.

Step 4 is the only part that needs a provider. Without one the answer is
**extractive**: the top passages verbatim, with their citations. That keeps
the chat surface useful on an air-gapped deployment and keeps the test
suite network-free — the same reason the 21.4 embedder and 25.4 captioner
each ship a stub path.

None of this touches the indexing path: no LLM call ever runs during a
sync, so re-indexing unchanged content stays byte-for-byte deterministic.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from syncsage.assistant.providers import PROVIDERS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are SyncSage, a research assistant answering strictly from a \
user's own indexed knowledge base.

Rules:
- Use ONLY the numbered passages provided. Never use outside knowledge.
- Cite every claim with the bracketed number of the passage it came from, \
like [1] or [2][5]. Put the citation right after the sentence it supports.
- If the passages do not answer the question, say so plainly and name what \
is missing. Do not guess, and do not pad the answer.
- Be concise and concrete. Lead with the answer, then the supporting detail.
- Prefer the user's own vocabulary from the passages over generic phrasing.
- Plain prose or short lists. No preamble like "Based on the passages"."""

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")

# Graph node types that carry meaning rather than structure — these are what
# a "surfaced fact" should point at.
FACT_NODE_TYPES = {"concept", "entity", "symbol", "external_reference"}
# Structural edges say "this file is in this folder"; they are noise as facts.
STRUCTURAL_EDGES = {"contains", "has_chunk", "indexes"}

EDGE_PHRASES = {
    "mentions": "mentions",
    "references": "references",
    "imports": "imports",
    "calls": "calls",
    "similar_to": "is similar to",
    "links_to": "links to",
    "derived_from": "is derived from",
}

# Object-first phrasing, for surfaces that lead with the concept and name the
# document afterwards ("idempotency — mentioned in docs/sync.md").
EDGE_PHRASES_PASSIVE = {
    "mentions": "mentioned in",
    "references": "referenced by",
    "imports": "imported by",
    "calls": "called by",
    "similar_to": "similar to",
    "links_to": "linked from",
    "derived_from": "source of",
}


def _snippet(result: dict, limit: int = 900) -> str:
    """Best available text for a hit: chunk preview, else summary."""
    chunks = result.get("chunks") or []
    for chunk in chunks:
        text = (chunk.get("text_preview") or "").strip()
        if text:
            return text[:limit]
    return str(result.get("summary") or result.get("label") or "").strip()[:limit]


def _title(result: dict) -> str:
    return str(
        result.get("title")
        or result.get("relative_path")
        or result.get("label")
        or result.get("node_id")
        or "untitled"
    )


def build_citations(results: list[dict], limit: int) -> list[dict]:
    """Numbered, de-duplicated citation records — one per source node."""
    citations: list[dict] = []
    seen: set[str] = set()
    for result in results:
        if len(citations) >= limit:
            break
        key = str(result.get("node_id") or result.get("chunk_id") or _title(result))
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "index": len(citations) + 1,
                "node_id": result.get("node_id"),
                "chunk_id": result.get("chunk_id"),
                "title": _title(result),
                "relative_path": result.get("relative_path"),
                "source_id": result.get("source_id")
                or (result.get("provenance") or {}).get("source_id"),
                "type": result.get("type"),
                "score": result.get("score"),
                "snippet": _snippet(result),
                "used": False,
            }
        )
    return citations


def passages_to_citations(passages: list, limit: int) -> list[dict]:
    """Numbered citation records from :class:`retrieval.Passage` objects.

    The workflow-facing twin of :func:`build_citations` — same output shape,
    so the API contract does not change with the workflow that produced it.
    """
    citations: list[dict] = []
    seen: set[str] = set()
    for passage in passages:
        if len(citations) >= limit:
            break
        key = passage.key()
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "index": len(citations) + 1,
                "node_id": passage.node_id,
                "chunk_id": passage.chunk_id,
                "title": passage.title,
                "relative_path": passage.relative_path,
                "source_id": passage.source_id,
                "type": passage.type,
                "score": passage.score,
                "snippet": passage.snippet,
                # How this passage was found — direct hit in some search mode,
                # or reached by walking the graph out of one.
                "retrieved_by": passage.mode,
                "used": False,
            }
        )
    return citations


def mark_used_citations(answer: str, citations: list[dict]) -> set[int]:
    """Flag the citations an answer actually cited. Returns the indices."""
    used = {int(n) for n in _CITATION_RE.findall(answer) if n.isdigit()}
    for citation in citations:
        citation["used"] = citation["index"] in used
    return used


def collect_facts(graph: Any, node_ids: list[str], limit: int = 12) -> list[dict]:
    """One-hop subject–predicate–object triples around the cited nodes.

    Collected **round-robin** across the cited nodes rather than depth-first
    through the first one. A well-connected document can easily have more
    one-hop concepts than the whole budget, and draining the budget on it
    produces a panel that says twelve things about a single file instead of
    one thing about each source the answer drew on — which is the opposite
    of what a "what did this answer touch" view is for.

    Deterministic: nodes keep citation order, edges keep graph order, so the
    same question over an unchanged graph yields the same facts in the same
    sequence.
    """
    if graph is None:
        return []

    def label_of(node_id: str) -> str:
        try:
            return str(graph.nodes[node_id].get("label") or node_id)
        except (KeyError, AttributeError):
            return node_id

    def type_of(node_id: str) -> str:
        try:
            return str(graph.nodes[node_id].get("type") or "")
        except (KeyError, AttributeError):
            return ""

    # Per-node candidate lists, in citation order.
    per_node: list[list[dict]] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id in node_ids:
        if node_id not in graph:
            continue
        subject = label_of(node_id)
        candidates: list[dict] = []
        for _src, target, edge_map in graph.out_edges(node_id):
            for data in edge_map.values():
                edge_type = str(data.get("type") or "")
                if edge_type in STRUCTURAL_EDGES:
                    continue
                if type_of(target) not in FACT_NODE_TYPES:
                    continue
                key = (node_id, edge_type, target)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "subject": subject,
                        "subject_id": node_id,
                        "predicate": EDGE_PHRASES.get(edge_type, edge_type.replace("_", " ")),
                        "predicate_passive": EDGE_PHRASES_PASSIVE.get(
                            edge_type, edge_type.replace("_", " ")
                        ),
                        "edge_type": edge_type,
                        "object": label_of(target),
                        "object_id": target,
                        "object_type": type_of(target),
                        "confidence": data.get("confidence"),
                    }
                )
        if candidates:
            per_node.append(candidates)

    facts: list[dict] = []
    depth = 0
    while len(facts) < limit and per_node:
        progressed = False
        for candidates in per_node:
            if depth >= len(candidates):
                continue
            facts.append(candidates[depth])
            progressed = True
            if len(facts) >= limit:
                break
        if not progressed:
            break
        depth += 1
    return facts


def short_reason(error: str) -> str:
    """One clause of a provider error, for the user-facing degraded answer."""
    first = error.strip().splitlines()[0]
    return first if len(first) <= 120 else first[:117].rstrip() + "…"


def build_prompt(question: str, citations: list[dict], facts: list[dict]) -> str:
    """Numbered passages + graph facts + the question."""
    lines = ["Passages from the knowledge base:", ""]
    for citation in citations:
        header = f"[{citation['index']}] {citation['title']}"
        if citation.get("relative_path") and citation["relative_path"] != citation["title"]:
            header += f" ({citation['relative_path']})"
        lines.append(header)
        lines.append(citation["snippet"] or "(no preview available)")
        lines.append("")
    if facts:
        lines.append("Relationships recorded in the knowledge graph:")
        for fact in facts:
            lines.append(f"- {fact['subject']} {fact['predicate']} {fact['object']}")
        lines.append("")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


def extractive_answer(question: str, citations: list[dict], reason: str | None = None) -> str:
    """Deterministic offline answer: the top passages, attributed.

    ``reason`` distinguishes the two ways to land here — no model configured,
    versus a configured model that failed. Reporting a provider outage as
    "no model is connected" sends the user to re-enter a key that was never
    the problem.
    """
    if not citations:
        return (
            "Nothing in the indexed sources matches that question. "
            "Add a source and sync it, or try different wording."
        )
    if reason:
        opening = (
            f"The chat model could not be reached ({reason}), so here are the top "
            f"matches for “{question.strip()}” straight from the index:"
        )
        closing = "Retrieval and citations are unaffected — retry for a written answer."
    else:
        opening = (
            f"No chat model is connected, so here are the top matches for "
            f"“{question.strip()}” straight from the index:"
        )
        closing = "Connect an API key to get a synthesized answer over these sources."
    lines = [opening, ""]
    for citation in citations[:3]:
        snippet = " ".join((citation["snippet"] or "").split())
        if len(snippet) > 400:
            snippet = snippet[:400].rstrip() + "…"
        lines.append(f"- {snippet} [{citation['index']}]")
    lines.append("")
    lines.append(closing)
    return "\n".join(lines)


def resolve_provider(config: Any, credential: Any, env: dict[str, str]) -> dict | None:
    """Pick the provider to call, session key first, else server env var.

    Returns ``{provider, api_key, model, base_url, source}`` or None when no
    credential is reachable (the extractive path).
    """
    settings = getattr(config, "assistant", None)
    if settings is not None and not getattr(settings, "enabled", True):
        return None

    if credential is not None:
        return {
            "provider": credential.provider,
            "api_key": credential.api_key,
            "model": credential.model or getattr(settings, "model", None),
            "base_url": credential.base_url or getattr(settings, "base_url", None),
            "source": "session",
        }

    configured = getattr(settings, "provider", "auto") or "auto"
    if configured == "none":
        return None
    if configured == "auto":
        from syncsage.assistant.providers import resolve_auto_provider

        configured = resolve_auto_provider(env)
        if configured is None:
            return None
    spec = PROVIDERS.get(configured)
    if spec is None:
        return None
    env_name = getattr(settings, "api_key_env", None) or spec.api_key_env
    api_key = env.get(env_name)
    if not api_key:
        return None
    return {
        "provider": configured,
        "api_key": api_key,
        "model": getattr(settings, "model", None),
        "base_url": getattr(settings, "base_url", None),
        "source": "environment",
    }


def answer_question(
    question: str,
    *,
    search: Any,
    knowledge_base: str,
    config: Any,
    graph: Any = None,
    state: Any = None,
    credential: Any = None,
    env: dict[str, str] | None = None,
    mode: str = "hybrid",
    max_results: int | None = None,
    source_name: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
    workflow: str | None = None,
    options: dict | None = None,
    on_step: Any = None,
) -> dict:
    """Answer ``question`` from the knowledge base, with citations and facts.

    This is the single entry point behind the UI chat panel, ``POST
    /assistant/chat`` and the MCP ``ask_knowledge_base`` tool. It resolves a
    credential, builds the retrieval toolbelt, and hands both to the selected
    :mod:`~syncsage.assistant.workflows` workflow — so which workflow runs is
    a configuration choice, not a code path.
    """
    import os

    from syncsage.assistant.llm import llm_from_selection
    from syncsage.assistant.retrieval import SyncSageRetriever
    from syncsage.assistant.workflows import (
        WorkflowRequest,
        build_workflow,
        resolve_workflow_name,
    )

    settings = getattr(config, "assistant", None)
    env = env if env is not None else dict(os.environ)
    max_results = max_results or int(getattr(settings, "max_context_chunks", 8) or 8)

    selected = resolve_provider(config, credential, env)
    llm = llm_from_selection(selected, settings)
    retriever = SyncSageRetriever(
        search=search,
        knowledge_base=knowledge_base,
        graph=graph,
        state=state,
        config=config,
    )

    name = resolve_workflow_name(
        workflow or getattr(settings, "workflow", "auto"), has_llm=llm is not None
    )
    merged_options = {
        "max_facts": int(getattr(settings, "max_facts", 12) or 12),
        **(dict(getattr(settings, "workflow_options", None) or {})),
        **(options or {}),
    }
    request = WorkflowRequest(
        question=question,
        mode=mode,
        max_results=max_results,
        source_name=source_name,
        principal=principal,
        principal_groups=principal_groups or [],
        options=merged_options,
        # Live progress for callers that want it (the streaming chat route).
        # None keeps the workflow byte-identical to before.
        on_step=on_step,
    )

    try:
        result = build_workflow(name).run(request, retriever, llm)
    except Exception as exc:  # a custom workflow must not take down the API
        logger.exception("assistant workflow %r failed; falling back to simple", name)
        from syncsage.assistant.workflows.simple import SimpleWorkflow

        result = SimpleWorkflow().run(request, retriever, llm)
        result.error = f"workflow {name!r} failed ({exc}); answered with the simple workflow"

    return {
        "question": question,
        "answer": result.answer,
        "mode": result.mode,
        "provider": result.provider,
        "model": result.model,
        "credential_source": selected.get("source") if selected else None,
        "error": result.error,
        "citations": result.citations,
        "facts": result.facts,
        "focus_node_ids": result.focus_node_ids,
        "search_mode": result.search_mode,
        "counts": result.counts,
        "workflow": result.workflow,
        "steps": [
            {"name": step.name, "detail": step.detail, "passages": step.passages}
            for step in result.steps
        ],
    }
