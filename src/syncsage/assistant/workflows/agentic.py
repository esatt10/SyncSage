"""The default agentic workflow, built as a LangGraph state graph.

```
        ┌────────┐
        │  plan  │◄──────────────┐
        └───┬────┘               │
            ▼                    │ evidence is thin and
      ┌──────────┐               │ budget remains
      │ retrieve │               │
      └────┬─────┘               │
           ▼                     │
      ┌─────────┐                │
      │ expand  │  graph walk    │
      └────┬────┘                │
           ▼                     │
       ┌───────┐                 │
       │ grade │─────────────────┘
       └───┬───┘
           ▼ sufficient / out of budget
     ┌─────────────┐     ┌────────┐
     │ synthesize  │────►│ verify │
     └─────────────┘     └────────┘
```

Why a graph and not a chain: the `grade → plan` edge is conditional. A
question whose first search comes back thin gets a *different* query — one
informed by what did come back — rather than the same query with a bigger
`k`. That loop is the whole reason to reach for an agent framework here.

**Every node fully exercises SyncSage's retrieval surface**, which a generic
RAG chain does not:

* `retrieve` fans out across `hybrid`, `vector` and `graph` modes, not one.
* `expand` walks the knowledge graph out of the best hits, reaching
  documents that share *no vocabulary* with the question but are connected
  through a concept, import or call edge.
* `plan` is told what the region can actually do (`RetrievalCapabilities`),
  so it never plans a semantic search against a region with no vector index.

**Everything is customizable.** Each node is a plain module-level function
taking and returning the state dict, `build_graph()` returns the compiled
LangGraph, and every knob is an `assistant.workflow_options` key. Swap a
node, re-wire the edges, or register your own workflow entirely — see
:mod:`syncsage.assistant.workflows`.

Requires ``pip install 'syncsage[agent]'``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypedDict

from syncsage.assistant.chat import (
    SYSTEM_PROMPT,
    build_prompt,
    extractive_answer,
    mark_used_citations,
    passages_to_citations,
    short_reason,
)
from syncsage.assistant.providers import ProviderError
from syncsage.assistant.workflows import WorkflowRequest, WorkflowResult, WorkflowStep

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # How many plan→retrieve→grade rounds before answering with what we have.
    "max_rounds": 2,
    # Search modes to fan out over. "vector" is dropped automatically when no
    # vector index is built, so leaving it on is safe.
    #
    # Deliberately NOT "hybrid" here. Hybrid = text + vector + a linear scan of
    # every graph node, and this loop issues one search per query per mode
    # across up to `max_rounds` rounds — on a 500k-node graph that scan
    # dominated everything else the agent did (measured: 10.3s per hybrid
    # search vs 0.03s text, 1.6s vector, 0.6s for a model call). The graph is
    # not lost by leaving it out: `expand_graph` below walks the graph out of
    # the best hits, which is the structural signal this loop actually wants.
    # Set `retrieval_modes: ["hybrid"]` per request to trade the latency back
    # for graph-scored candidates.
    "retrieval_modes": ["text", "vector"],
    # Walk the knowledge graph out of the best hits for related material.
    "expand_graph": True,
    "expand_depth": 1,
    "expand_per_node": 3,
    # Passages fetched per query per mode.
    "per_query_results": 6,
    # Total passages offered to the synthesis step.
    "max_context_passages": 10,
    # Ask the model to grade its own evidence before answering.
    "grade_evidence": True,
    # Drop [n] markers that do not resolve to a real citation.
    "verify_citations": True,
    "max_facts": 12,
}

PLANNER_SYSTEM = """You plan retrieval over a private knowledge base. \
Given a question and a description of what the knowledge base contains, \
produce the search queries most likely to surface the answer.

Reply with JSON only, no prose:
{"queries": ["...", "..."], "modes": ["hybrid"], "reasoning": "one short line"}

Rules:
- 1 to 3 queries. Use the user's own likely vocabulary and, where the \
question is compound, split it into its parts.
- Do not restate the question verbatim as the only query; add the specific \
terms a document answering it would contain.
- "modes" may include only the modes listed as available.
- Prefer "vector" when the question is conceptual and "text" when it names \
an exact identifier, path or symbol."""

GRADER_SYSTEM = """You judge whether retrieved passages are sufficient to \
answer a question.

Reply with JSON only, no prose:
{"sufficient": true|false, "missing": "what is absent", "next_query": "a \
better search query, or empty"}

Be strict about sufficiency but realistic: if the passages substantially \
answer the question, say true. Only say false when a specific, nameable \
piece of information is missing that a different search might find."""


class AgentState(TypedDict, total=False):
    """The state LangGraph threads through the nodes."""

    question: str
    options: dict
    capabilities: Any
    queries: list[str]
    modes: list[str]
    passages: list
    round: int
    plan_notes: list[str]
    grade: dict
    citations: list[dict]
    facts: list[dict]
    answer: str
    answer_mode: str
    error: str | None
    steps: list[WorkflowStep]


# --------------------------------------------------------------------- nodes
# Each node is a plain function of (state, ctx) -> partial state. `ctx` carries
# the retriever and llm. Override any of them by name via
# assistant.workflow_options["nodes"], or import and reuse them in your own
# graph.


def plan_node(state: AgentState, ctx: dict) -> dict:
    """Decide what to search for, and in which modes."""
    retriever, llm = ctx["retriever"], ctx["llm"]
    options = state["options"]
    capabilities = state.get("capabilities") or retriever.capabilities()
    question = state["question"]
    round_index = state.get("round", 0)

    available = [m for m in options["retrieval_modes"] if m in capabilities.modes]
    if not available:
        available = ["hybrid"]

    # A follow-up round already knows what was missing — use the grader's
    # suggestion rather than re-planning from scratch.
    previous = state.get("grade") or {}
    if round_index > 0 and previous.get("next_query"):
        return {
            "queries": [str(previous["next_query"])],
            "modes": available,
            "capabilities": capabilities,
            "plan_notes": [*state.get("plan_notes", []), f"refined: {previous['next_query']}"],
            "steps": [
                *state.get("steps", []),
                WorkflowStep(
                    name="replan",
                    detail=f"evidence was thin ({previous.get('missing', 'unclear')}); "
                    f"searching for “{previous['next_query']}”",
                ),
            ],
        }

    queries = [question]
    notes = "asked as-is"
    if llm is not None:
        raw = llm.try_complete(
            PLANNER_SYSTEM,
            f"{capabilities.as_prompt_context()}\n\nQuestion: {question}",
            max_output_tokens=400,
        )
        parsed = _parse_json(raw)
        if parsed:
            planned = [str(q).strip() for q in parsed.get("queries", []) if str(q).strip()]
            if planned:
                # Always keep the original question: a planner that drifts
                # should not be able to lose the user's actual words.
                queries = _dedupe([question, *planned])[:4]
                notes = str(parsed.get("reasoning") or "planned")
            planned_modes = [str(m) for m in parsed.get("modes", []) if m in capabilities.modes]
            if planned_modes:
                available = planned_modes

    return {
        "queries": queries,
        "modes": available,
        "capabilities": capabilities,
        "plan_notes": [*state.get("plan_notes", []), notes],
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="plan",
                detail=f"{notes} → {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
                f"across {', '.join(available)}",
            ),
        ],
    }


def retrieve_node(state: AgentState, ctx: dict) -> dict:
    """Fan out across every planned query and mode, then merge."""
    retriever = ctx["retriever"]
    request: WorkflowRequest = ctx["request"]
    options = state["options"]

    found = retriever.multi_search(
        state.get("queries") or [state["question"]],
        modes=state.get("modes") or ["hybrid"],
        limit=int(options["per_query_results"]),
        source_name=request.source_name,
        principal=request.principal,
        principal_groups=request.principal_groups,
    )
    merged = _merge_passages(state.get("passages", []), found)
    return {
        "passages": merged,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="retrieve",
                detail=f"{len(found)} passages from {len(state.get('queries') or [])} "
                f"quer{'y' if len(state.get('queries') or []) == 1 else 'ies'}",
                passages=len(found),
            ),
        ],
    }


def expand_node(state: AgentState, ctx: dict) -> dict:
    """Walk the knowledge graph out of the best hits.

    This is the step a lexical or vector-only pipeline cannot do: a document
    that shares no words with the question is still reachable through the
    concepts, imports and calls SyncSage recorded at index time.
    """
    retriever = ctx["retriever"]
    options = state["options"]
    if not options["expand_graph"]:
        return {}
    passages = state.get("passages", [])
    if not passages:
        return {}
    related = retriever.expand(
        passages[:4],
        depth=int(options["expand_depth"]),
        per_node=int(options["expand_per_node"]),
    )
    if not related:
        return {}
    return {
        "passages": _merge_passages(passages, related),
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="expand",
                detail=f"followed graph edges to {len(related)} related document(s)",
                passages=len(related),
            ),
        ],
    }


def grade_node(state: AgentState, ctx: dict) -> dict:
    """Decide whether the evidence answers the question."""
    llm = ctx["llm"]
    options = state["options"]
    passages = state.get("passages", [])
    round_index = state.get("round", 0) + 1

    # Deterministic floor: nothing found is definitively insufficient, and
    # with no model there is nobody to ask, so take what we have.
    if not passages:
        return {
            "round": round_index,
            "grade": {"sufficient": False, "missing": "no matching passages", "next_query": ""},
        }
    if llm is None or not options["grade_evidence"]:
        return {
            "round": round_index,
            "grade": {"sufficient": True, "missing": "", "next_query": ""},
        }

    evidence = "\n\n".join(
        f"[{i + 1}] {p.title}\n{p.snippet[:500]}" for i, p in enumerate(passages[:8])
    )
    raw = llm.try_complete(
        GRADER_SYSTEM,
        f"Question: {state['question']}\n\nPassages:\n{evidence}",
        max_output_tokens=300,
    )
    parsed = _parse_json(raw) or {"sufficient": True}
    grade = {
        "sufficient": bool(parsed.get("sufficient", True)),
        "missing": str(parsed.get("missing") or ""),
        "next_query": str(parsed.get("next_query") or ""),
    }
    return {
        "round": round_index,
        "grade": grade,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="grade",
                detail="evidence is sufficient"
                if grade["sufficient"]
                else f"missing: {grade['missing'] or 'unclear'}",
                passages=len(passages),
            ),
        ],
    }


def synthesize_node(state: AgentState, ctx: dict) -> dict:
    """Write the grounded answer over the accumulated evidence."""
    retriever, llm = ctx["retriever"], ctx["llm"]
    options = state["options"]
    passages = state.get("passages", [])[: int(options["max_context_passages"])]
    citations = passages_to_citations(passages, int(options["max_context_passages"]))
    node_ids = [c["node_id"] for c in citations if c.get("node_id")]
    facts = retriever.facts(node_ids, int(options["max_facts"]))

    if llm is None or not citations:
        return {
            "citations": citations,
            "facts": facts,
            "answer": extractive_answer(state["question"], citations),
            "answer_mode": "extractive",
        }
    try:
        answer = llm.complete(SYSTEM_PROMPT, build_prompt(state["question"], citations, facts))
        return {
            "citations": citations,
            "facts": facts,
            "answer": answer,
            "answer_mode": "llm",
            "steps": [
                *state.get("steps", []),
                WorkflowStep(
                    name="synthesize",
                    detail=f"answered from {len(citations)} passages",
                    passages=len(citations),
                ),
            ],
        }
    except ProviderError as exc:
        return {
            "citations": citations,
            "facts": facts,
            "answer": extractive_answer(
                state["question"], citations, reason=short_reason(str(exc))
            ),
            "answer_mode": "extractive",
            "error": str(exc),
        }


def verify_node(state: AgentState, ctx: dict) -> dict:
    """Strip citation markers that do not resolve to a real passage.

    A model asked to cite `[n]` will occasionally invent an `n` beyond the
    passages it was given. Emitting that unchecked would put a link in the UI
    that goes nowhere, which is worse than no citation at all.
    """
    options = state["options"]
    answer = state.get("answer", "")
    citations = state.get("citations", [])
    if not options["verify_citations"] or not answer:
        mark_used_citations(answer, citations)
        return {}

    valid = {c["index"] for c in citations}
    dangling: set[int] = set()

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index in valid:
            return match.group(0)
        dangling.add(index)
        return ""

    cleaned = re.sub(r"\[(\d{1,2})\]", replace, answer)
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    mark_used_citations(cleaned, citations)
    if not dangling:
        return {"answer": cleaned}
    return {
        "answer": cleaned,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="verify",
                detail=f"dropped {len(dangling)} citation marker(s) with no matching passage",
            ),
        ],
    }


def should_retry(state: AgentState, ctx: dict) -> str:
    """Conditional edge: loop back to planning, or go answer."""
    options = state["options"]
    grade = state.get("grade") or {}
    if grade.get("sufficient", True):
        return "synthesize"
    if state.get("round", 0) >= int(options["max_rounds"]):
        return "synthesize"
    if not grade.get("next_query") and not state.get("passages"):
        # Nothing found and no idea what else to try — another identical
        # round would just burn a model call.
        return "synthesize"
    return "plan"


NODES = {
    "plan": plan_node,
    "retrieve": retrieve_node,
    "expand": expand_node,
    "grade": grade_node,
    "synthesize": synthesize_node,
    "verify": verify_node,
}


def build_graph(options: dict[str, Any], nodes: dict[str, Any] | None = None):
    """Compile the LangGraph state graph.

    Import this to inspect or modify the default topology::

        from syncsage.assistant.workflows.agentic import build_graph, NODES
        graph = build_graph(options, nodes={**NODES, "grade": my_grader})
    """
    from langgraph.graph import END, START, StateGraph

    nodes = nodes or NODES
    builder = StateGraph(AgentState)
    for name in ("plan", "retrieve", "expand", "grade", "synthesize", "verify"):
        # LangGraph calls node(state, config); ctx rides on the config so the
        # nodes stay plain, testable functions of (state, ctx).
        builder.add_node(
            name,
            _bind(nodes[name]),
        )
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "expand")
    builder.add_edge("expand", "grade")
    builder.add_conditional_edges(
        "grade",
        _bind_router(should_retry),
        {"plan": "plan", "synthesize": "synthesize"},
    )
    builder.add_edge("synthesize", "verify")
    builder.add_edge("verify", END)
    return builder.compile()


def _bind(fn):
    def node(state, config):
        ctx = config["configurable"]["ctx"]
        before = len(state.get("steps") or [])
        result = fn(state, ctx)
        # Publish whatever this node appended, the moment it appended it. The
        # loop can take a minute over a large index, and "planning… retrieving
        # 35 passages… grading" is the difference between waiting and
        # wondering whether it hung. Nodes return the whole steps list, so
        # anything past the incoming length is new.
        request = ctx.get("request")
        if request is not None and isinstance(result, dict):
            for step in (result.get("steps") or [])[before:]:
                request.report(step)
        return result

    node.__name__ = getattr(fn, "__name__", "node")
    return node


def _bind_router(fn):
    def router(state, config):
        return fn(state, config["configurable"]["ctx"])

    router.__name__ = getattr(fn, "__name__", "router")
    return router


class AgenticWorkflow:
    """Plan → retrieve → expand → grade → (loop) → synthesize → verify."""

    name = "agentic"

    def __init__(self, nodes: dict[str, Any] | None = None) -> None:
        self._nodes = nodes

    def run(self, request: WorkflowRequest, retriever: Any, llm: Any) -> WorkflowResult:
        options = {**DEFAULTS, **(request.options or {})}
        options["max_context_passages"] = max(
            int(options["max_context_passages"]), int(request.max_results)
        )
        nodes = self._nodes or {**NODES, **(options.get("nodes") or {})}

        try:
            graph = build_graph(options, nodes)
        except ImportError as exc:  # extra not installed after all
            logger.warning("langgraph unavailable (%s); falling back to the simple workflow", exc)
            from syncsage.assistant.workflows.simple import SimpleWorkflow

            return SimpleWorkflow().run(request, retriever, llm)

        ctx = {"retriever": retriever, "llm": llm, "request": request}
        initial: AgentState = {
            "question": request.question,
            "options": options,
            "passages": [],
            "round": 0,
            "plan_notes": [],
            "steps": [],
        }
        # `recursion_limit` is LangGraph's own runaway guard; size it to the
        # configured rounds so a pathological grader cannot spin forever.
        final = graph.invoke(
            initial,
            config={
                "configurable": {"ctx": ctx},
                "recursion_limit": 6 * max(1, int(options["max_rounds"])) + 8,
            },
        )

        citations = final.get("citations", [])
        return WorkflowResult(
            answer=final.get("answer", ""),
            citations=citations,
            facts=final.get("facts", []),
            focus_node_ids=[c["node_id"] for c in citations if c.get("node_id")],
            mode=final.get("answer_mode", "extractive"),
            provider=llm.provider if llm else None,
            model=llm.model_id if llm else None,
            error=final.get("error"),
            search_mode="+".join(final.get("modes", [request.mode])),
            counts={
                "rounds": final.get("round", 0),
                "passages": len(final.get("passages", [])),
                "citations": len(citations),
            },
            steps=final.get("steps", []),
            workflow=self.name,
        )


# ------------------------------------------------------------------ helpers


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


def _merge_passages(existing: list, incoming: list) -> list:
    """Union by passage key, keeping the strongest score; stable order."""
    merged = {p.key(): p for p in existing}
    for passage in incoming:
        current = merged.get(passage.key())
        if current is None or passage.score > current.score:
            merged[passage.key()] = passage
    return sorted(merged.values(), key=lambda p: -p.score)


def _parse_json(raw: str | None) -> dict | None:
    """Parse a JSON object out of a model reply, tolerating code fences."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
