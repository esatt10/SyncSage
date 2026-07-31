"""Pluggable question-answering workflows.

Acceptance:

1. The registry resolves built-ins, entry-point plugins and programmatic
   registrations, with the same precedence rules as the 31.1 connector SDK.
2. ``auto`` picks the agent only when it can actually help (LangGraph
   installed *and* a model reachable); everything degrades to ``simple``.
3. The LangGraph agent really is a graph, not a chain: a thin first round
   loops back through ``plan`` with a *different* query.
4. It exercises SyncSage's whole retrieval surface — multiple modes, and a
   graph walk that reaches documents lexical search never returned.
5. It verifies its own citations, dropping ``[n]`` markers with no passage.
6. Every path works with no model at all, and a broken custom workflow
   cannot take down question answering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncsage.api.app import create_app
from syncsage.assistant import providers as providers_module
from syncsage.assistant.llm import LLM
from syncsage.assistant.retrieval import SyncSageRetriever
from syncsage.assistant.workflows import (
    WorkflowRequest,
    WorkflowResult,
    build_workflow,
    langgraph_available,
    list_workflows,
    register_workflow,
    reset_workflow_registry,
    resolve_workflow_name,
)
from syncsage.assistant.workflows.simple import SimpleWorkflow
from syncsage.graph.simple import SimpleMultiDiGraph

langgraph = pytest.importorskip if False else None  # keep the import list tidy


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_workflow_registry()
    yield
    reset_workflow_registry()


# ------------------------------------------------------------------ fixtures


class _FakeSearch:
    """A tiny corpus that responds differently per query and mode."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.vector = object()  # pretend a vector index exists

    def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
        self.calls.append((query, mode))
        corpus = {
            "hash": [_hit("docs/hashing.md", "Artifacts are hashed with sha256.", 2.0)],
            "skip": [_hit("docs/skip.md", "Unchanged files are skipped on sync.", 1.8)],
        }
        results = []
        for keyword, hits in corpus.items():
            if keyword in query.lower():
                results.extend(hits)
        if not results:
            results = [_hit("docs/overview.md", "SyncSage indexes sources.", 1.0)]
        return {"query": query, "mode": mode, "results": results[:max_results], "counts": {}}


def _hit(path: str, text: str, score: float) -> dict:
    return {
        "node_id": f"file:kb:{path}",
        "chunk_id": f"chunk:kb:{path}:0",
        "type": "chunk",
        "title": path,
        "relative_path": path,
        "score": score,
        "chunks": [{"text_preview": text}],
    }


def _graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    for path in ("docs/hashing.md", "docs/skip.md", "docs/overview.md"):
        graph.add_node(f"file:kb:{path}", id=path, type="file", label=path)
    # A document reachable ONLY through a graph edge — no search query
    # returns it, so if it shows up it came from the graph walk.
    graph.add_node(
        "file:kb:docs/manifest.md",
        id="manifest",
        type="file",
        label="docs/manifest.md",
        summary="Manifests record what was indexed.",
    )
    graph.add_edge("file:kb:docs/hashing.md", "file:kb:docs/manifest.md", type="references")
    graph.add_node("concept:kb:idempotency", id="c", type="concept", label="idempotency")
    graph.add_edge("file:kb:docs/hashing.md", "concept:kb:idempotency", type="mentions")
    return graph


def _retriever(search=None, graph=None) -> SyncSageRetriever:
    return SyncSageRetriever(
        search=search or _FakeSearch(),
        knowledge_base="kb",
        graph=graph if graph is not None else _graph(),
        state=None,
        config=None,
    )


class _ScriptedLLM(LLM):
    """An LLM whose replies are chosen by which system prompt arrives."""

    def __init__(self, script):
        super().__init__(provider="openai", api_key="k", model="test-model")
        self.script = script
        self.seen: list[str] = []

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        for marker, reply in self.script:
            if marker in system:
                self.seen.append(marker)
                return reply(prompt) if callable(reply) else reply
        raise AssertionError(f"unscripted system prompt: {system[:60]}")


# ------------------------------------------------------------------ registry


def test_builtin_workflows_are_listed() -> None:
    names = {entry["name"] for entry in list_workflows()}
    assert "simple" in names
    if langgraph_available():
        assert "agentic" in names


def test_programmatic_registration_wins_and_resolves() -> None:
    class Custom:
        def run(self, request, retriever, llm):
            return WorkflowResult(answer="custom answer", workflow="custom")

    register_workflow("custom", Custom)
    assert "custom" in {entry["name"] for entry in list_workflows()}

    result = build_workflow("custom").run(WorkflowRequest(question="x"), _retriever(), None)
    assert result.answer == "custom answer"


def test_registering_a_non_workflow_is_rejected() -> None:
    with pytest.raises(TypeError):
        register_workflow("bad", "not a workflow")
    with pytest.raises(ValueError):
        register_workflow("", SimpleWorkflow)


def test_unknown_workflow_falls_back_to_simple() -> None:
    workflow = build_workflow("does-not-exist")
    assert isinstance(workflow, SimpleWorkflow)


@pytest.mark.parametrize(
    ("configured", "has_llm", "expected"),
    [
        ("simple", True, "simple"),
        ("simple", False, "simple"),
        ("agentic", False, "agentic"),  # explicit choice is honored
        ("auto", False, "simple"),  # no model: a planner has nothing to plan with
        ("my-plugin", True, "my-plugin"),
    ],
)
def test_auto_resolution(configured, has_llm, expected) -> None:
    assert resolve_workflow_name(configured, has_llm=has_llm) == expected


def test_auto_prefers_the_agent_when_it_can_help() -> None:
    expected = "agentic" if langgraph_available() else "simple"
    assert resolve_workflow_name("auto", has_llm=True) == expected


# ----------------------------------------------------------------- retrieval


def test_retriever_merges_across_queries_and_modes_deterministically() -> None:
    retriever = _retriever()
    first = retriever.multi_search(["hash", "skip"], modes=["hybrid", "vector"], limit=5)
    second = retriever.multi_search(["hash", "skip"], modes=["hybrid", "vector"], limit=5)

    assert [p.key() for p in first] == [p.key() for p in second]
    assert {p.title for p in first} >= {"docs/hashing.md", "docs/skip.md"}
    # A passage found by two modes records both.
    multi = [p.mode for p in first if "+" in p.mode]
    assert multi
    # …and always under one label. The mode string is shown to the user, so
    # two passages found by the same pair must not read "hybrid+vector" and
    # "vector+hybrid" depending on which query happened to hit first.
    assert all(mode == "hybrid+vector" for mode in multi)


def test_retriever_caches_identical_searches() -> None:
    search = _FakeSearch()
    retriever = _retriever(search=search)
    retriever.search("hash", mode="hybrid", limit=5)
    retriever.search("hash", mode="hybrid", limit=5)
    assert search.calls.count(("hash", "hybrid")) == 1


def test_graph_expansion_reaches_documents_search_cannot() -> None:
    """The capability a lexical/vector-only pipeline does not have."""
    retriever = _retriever()
    hits = retriever.search("hash", limit=5)
    assert all(p.title != "docs/manifest.md" for p in hits), "must be unreachable by search"

    related = retriever.expand(hits, depth=1)

    assert "docs/manifest.md" in {p.title for p in related}
    assert all(p.mode == "graph-expand" for p in related)
    # Derived evidence must rank below the direct hit it came from.
    assert all(p.score < hits[0].score for p in related)


def test_capabilities_describe_what_the_region_can_do() -> None:
    caps = _retriever().capabilities()
    assert "hybrid" in caps.modes and "graph" in caps.modes and "vector" in caps.modes
    assert "Search modes available" in caps.as_prompt_context()

    no_vector = SyncSageRetriever(
        search=type("S", (), {"search_context": lambda *a, **k: {"results": []}})(),
        knowledge_base="kb",
        graph=None,
    )
    assert "vector" not in no_vector.capabilities().modes
    assert "not enabled" in no_vector.capabilities().as_prompt_context()


# -------------------------------------------------------------------- simple


def test_simple_workflow_answers_without_a_model() -> None:
    result = SimpleWorkflow().run(WorkflowRequest(question="hash"), _retriever(), None)

    assert result.workflow == "simple"
    assert result.mode == "extractive"
    assert result.citations
    assert [step.name for step in result.steps] == ["retrieve"]


def test_simple_workflow_synthesizes_with_a_model() -> None:
    llm = _ScriptedLLM([("research assistant", "Hashes make it idempotent [1].")])
    result = SimpleWorkflow().run(WorkflowRequest(question="hash"), _retriever(), llm)

    assert result.mode == "llm"
    assert result.citations[0]["used"] is True
    assert [step.name for step in result.steps] == ["retrieve", "answer"]


# ------------------------------------------------------------------- agentic


@pytest.fixture()
def agentic():
    pytest.importorskip("langgraph")
    from syncsage.assistant.workflows.agentic import AgenticWorkflow

    return AgenticWorkflow()


def test_agentic_loops_back_when_evidence_is_thin(agentic) -> None:
    """The conditional edge — the reason this is a graph and not a chain."""
    grades = iter(
        [
            json.dumps({"sufficient": False, "missing": "the skip rule", "next_query": "skip"}),
            json.dumps({"sufficient": True, "missing": "", "next_query": ""}),
        ]
    )
    llm = _ScriptedLLM(
        [
            ("plan retrieval", json.dumps({"queries": ["hash"], "modes": ["hybrid"]})),
            ("judge whether", lambda _prompt: next(grades)),
            ("research assistant", "Hashing [1] and skipping [2] make it idempotent."),
        ]
    )
    search = _FakeSearch()

    result = agentic.run(WorkflowRequest(question="why idempotent"), _retriever(search), llm)

    names = [step.name for step in result.steps]
    assert "replan" in names, f"expected a second round, got {names}"
    assert result.counts["rounds"] == 2
    # The refined query actually reached the index.
    assert any(query == "skip" for query, _mode in search.calls)
    assert result.workflow == "agentic"


def test_agentic_stops_at_the_round_budget(agentic) -> None:
    """A grader that is never satisfied must not loop forever."""
    llm = _ScriptedLLM(
        [
            ("plan retrieval", json.dumps({"queries": ["hash"], "modes": ["hybrid"]})),
            (
                "judge whether",
                json.dumps({"sufficient": False, "missing": "more", "next_query": "again"}),
            ),
            ("research assistant", "Partial answer [1]."),
        ]
    )

    result = agentic.run(
        WorkflowRequest(question="why", options={"max_rounds": 2}), _retriever(), llm
    )

    assert result.counts["rounds"] <= 2
    assert result.answer, "must still answer with whatever it has"


def test_agentic_verifies_citations(agentic) -> None:
    """A model citing a passage it was never given must not reach the UI."""
    llm = _ScriptedLLM(
        [
            ("plan retrieval", json.dumps({"queries": ["hash"], "modes": ["hybrid"]})),
            ("judge whether", json.dumps({"sufficient": True})),
            ("research assistant", "Real claim [1]. Invented claim [98]."),
        ]
    )

    result = agentic.run(WorkflowRequest(question="hash"), _retriever(), llm)

    assert "[98]" not in result.answer
    assert "[1]" in result.answer
    assert any(step.name == "verify" for step in result.steps)


def test_agentic_uses_the_graph_walk(agentic) -> None:
    llm = _ScriptedLLM(
        [
            ("plan retrieval", json.dumps({"queries": ["hash"], "modes": ["hybrid"]})),
            ("judge whether", json.dumps({"sufficient": True})),
            ("research assistant", "Answer [1]."),
        ]
    )

    result = agentic.run(WorkflowRequest(question="hash"), _retriever(), llm)

    assert any(step.name == "expand" for step in result.steps)
    titles = {c["title"] for c in result.citations}
    assert "docs/manifest.md" in titles, "graph-only document should reach the citations"


def test_agentic_works_with_no_model_at_all(agentic) -> None:
    """Offline: no planner, no grader, still a grounded extractive answer."""
    result = agentic.run(WorkflowRequest(question="hash"), _retriever(), None)

    assert result.mode == "extractive"
    assert result.citations
    assert result.workflow == "agentic"


def test_agentic_survives_an_unreachable_planner(agentic, monkeypatch) -> None:
    """A planner outage degrades the plan, it does not fail the question."""

    class FlakyLLM(_ScriptedLLM):
        def complete(self, system, prompt, **kwargs):
            if "research assistant" in system:
                return "Answer [1]."
            raise providers_module.ProviderError("503 planner down")

    result = agentic.run(WorkflowRequest(question="hash"), _retriever(), FlakyLLM([]))

    assert result.mode == "llm"
    assert result.citations


def test_agentic_json_parsing_tolerates_code_fences() -> None:
    pytest.importorskip("langgraph")
    from syncsage.assistant.workflows.agentic import _parse_json

    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    assert _parse_json("not json at all") is None
    assert _parse_json(None) is None


def test_nodes_are_replaceable(agentic) -> None:
    """Full customization: swap a node, keep the rest of the graph."""
    pytest.importorskip("langgraph")
    from syncsage.assistant.workflows.agentic import NODES, AgenticWorkflow

    seen = {}

    def my_plan(state, ctx):
        seen["called"] = True
        return {"queries": ["skip"], "modes": ["hybrid"], "plan_notes": ["custom planner"]}

    result = AgenticWorkflow(nodes={**NODES, "plan": my_plan}).run(
        WorkflowRequest(question="anything"), _retriever(), None
    )

    assert seen.get("called") is True
    assert "docs/skip.md" in {c["title"] for c in result.citations}


# ----------------------------------------------------------------- HTTP/MCP


def test_workflows_route_describes_the_deployment(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    payload = client.get("/assistant/workflows").json()

    assert {"simple"} <= {entry["name"] for entry in payload["workflows"]}
    assert payload["configured"] == "auto"
    assert payload["active"] in ("simple", "agentic")
    assert payload["agent_extra_installed"] is langgraph_available()
    assert "max_rounds" in payload["option_defaults"]["agentic"]


def test_chat_can_select_a_workflow_per_request(loaded_config, workspace_copy: Path) -> None:
    loaded_config.syncsage.workspace_root = workspace_copy
    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    client = TestClient(app)

    body = client.post(
        "/assistant/chat", json={"question": "sync engine", "workflow": "simple"}
    ).json()

    assert body["workflow"] == "simple"
    assert body["steps"], "the trace must reach the client"


def test_a_broken_custom_workflow_cannot_break_chat(loaded_config) -> None:
    class Exploding:
        def run(self, request, retriever, llm):
            raise RuntimeError("boom")

    register_workflow("exploding", Exploding)
    client = TestClient(create_app(config=loaded_config))

    response = client.post(
        "/assistant/chat", json={"question": "anything", "workflow": "exploding"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow"] == "simple", "falls back rather than 500-ing"
    assert "exploding" in body["error"]


def test_ask_knowledge_base_is_exposed_over_mcp(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    names = {tool["name"] for tool in client.get("/mcp/info").json()["tools"]}
    assert "ask_knowledge_base" in names
    # Rule 8: the pre-existing tool surface is additive-only.
    assert {"search_context", "sync_source", "get_graph_neighbors"} <= names
