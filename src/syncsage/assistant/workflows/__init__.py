"""Pluggable question-answering workflows.

A *workflow* turns a question into a grounded answer. SyncSage ships two:

* ``simple`` — one retrieval pass, one model call. Zero extra dependencies,
  always available, and the behavior the chat surface has had since it
  existed.
* ``agentic`` — a **LangGraph** state graph that plans, retrieves across
  every search mode, walks the knowledge graph for material lexical search
  missed, grades its own evidence, loops when it is thin, and verifies the
  citations it emits. Requires ``pip install 'syncsage[agent]'``.

Both are ordinary registrations, so a third one you write is a first-class
citizen rather than a fork. Register programmatically::

    from syncsage.assistant.workflows import register_workflow
    register_workflow("my-flow", MyWorkflow)

…or ship it in a package, exactly like the 31.1 connector SDK::

    [project.entry-points."syncsage.agent_workflows"]
    my-flow = "my_pkg.flows:MyWorkflow"

then select it with ``assistant.workflow: my-flow`` (or per request). A
workflow only has to implement :meth:`Workflow.run`; everything it needs to
reach the index arrives as a :class:`~syncsage.assistant.retrieval.SyncSageRetriever`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "syncsage.agent_workflows"


@dataclass
class WorkflowRequest:
    """Everything a workflow is asked to answer, and under what limits."""

    question: str
    mode: str = "hybrid"
    max_results: int = 8
    source_name: str | None = None
    principal: str | None = None
    principal_groups: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    #: Called with each :class:`WorkflowStep` the moment it completes, so a
    #: caller can show progress instead of a spinner. Optional and best-effort:
    #: a workflow that ignores it still works, and an exception raised by the
    #: callback must never fail the answer.
    on_step: Callable[[WorkflowStep], None] | None = None

    def report(self, step: WorkflowStep) -> None:
        """Publish one completed step. Never raises."""

        if self.on_step is None:
            return
        try:
            self.on_step(step)
        except Exception:  # pragma: no cover - progress is never load-bearing
            logger.debug("progress callback failed", exc_info=True)


@dataclass
class WorkflowStep:
    """One recorded step, so the UI can show what the agent actually did."""

    name: str
    detail: str
    passages: int = 0


@dataclass
class WorkflowResult:
    """A workflow's answer, in the shape the API and UI already speak."""

    answer: str
    citations: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    focus_node_ids: list[str] = field(default_factory=list)
    mode: str = "extractive"
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    search_mode: str = "hybrid"
    counts: dict[str, Any] = field(default_factory=dict)
    # The agent's trace. Empty for single-shot workflows.
    steps: list[WorkflowStep] = field(default_factory=list)
    workflow: str = "simple"


@runtime_checkable
class Workflow(Protocol):
    """The one method a question-answering workflow must implement."""

    def run(self, request: WorkflowRequest, retriever: Any, llm: Any) -> WorkflowResult:
        """Answer ``request`` using ``retriever``; ``llm`` may be None.

        ``llm`` is a :class:`~syncsage.assistant.llm.LLM` or ``None`` when no
        provider is reachable. A workflow **must** still return a useful
        result in that case — SyncSage is expected to work offline, and the
        retrieval half of the answer does not need a model.
        """
        ...


_programmatic: dict[str, Any] = {}
_entry_point_cache: dict[str, Any] | None = None


def register_workflow(name: str, factory: Any) -> None:
    """Register a workflow factory under ``name``.

    ``factory`` is anything callable that returns an object with ``run`` —
    a class, a function, or an instance that is already a Workflow.
    Programmatic registrations win over entry points of the same name so a
    test or an embedding application can always pin behavior.
    """
    if not name or not isinstance(name, str):
        raise ValueError("workflow name must be a non-empty string")
    if not (callable(factory) or hasattr(factory, "run")):
        raise TypeError(
            f"{factory!r} is not usable as a workflow: pass a class, a factory "
            "callable, or an object with a .run(request, retriever, llm) method"
        )
    _programmatic[name] = factory


def _load_entry_points() -> dict[str, Any]:
    global _entry_point_cache
    if _entry_point_cache is None:
        loaded: dict[str, Any] = {}
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded[ep.name] = ep.load()
            except Exception:  # a broken plugin must not break question answering
                logger.warning("agent workflow plugin %r failed to import; skipping", ep.name)
        _entry_point_cache = loaded
    return _entry_point_cache


def _builtins() -> dict[str, Any]:
    from syncsage.assistant.workflows.simple import SimpleWorkflow

    registry: dict[str, Any] = {"simple": SimpleWorkflow}
    if langgraph_available():
        from syncsage.assistant.workflows.agentic import AgenticWorkflow

        registry["agentic"] = AgenticWorkflow
    return registry


def langgraph_available() -> bool:
    """True when the ``[agent]`` extra is installed."""
    from importlib.util import find_spec

    try:
        return find_spec("langgraph") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def get_workflow(name: str) -> Any | None:
    """Resolve a workflow factory by name, or None."""
    if name in _programmatic:
        return _programmatic[name]
    plugins = _load_entry_points()
    if name in plugins:
        return plugins[name]
    return _builtins().get(name)


def list_workflows() -> list[dict[str, Any]]:
    """Every resolvable workflow, with enough detail for a UI picker."""
    described = {
        "simple": (
            "Single pass",
            "One retrieval, one model call. Fast, predictable, no extra dependencies.",
        ),
        "agentic": (
            "Agentic (LangGraph)",
            "Plans, searches every mode, walks the graph for related material, "
            "grades its own evidence and retries when it is thin, then verifies citations.",
        ),
    }
    names = set(_builtins()) | set(_load_entry_points()) | set(_programmatic)
    out = []
    for name in sorted(names):
        label, description = described.get(name, (name, "Custom workflow."))
        out.append(
            {
                "name": name,
                "label": label,
                "description": description,
                "builtin": name in ("simple", "agentic"),
            }
        )
    return out


def resolve_workflow_name(configured: str | None, *, has_llm: bool) -> str:
    """Turn ``assistant.workflow`` into a concrete name.

    ``auto`` means "the best available": the agentic graph when LangGraph is
    installed *and* a model is reachable (a planner with no model to plan
    with is just overhead), otherwise the single-pass workflow.
    """
    configured = (configured or "auto").strip() or "auto"
    if configured != "auto":
        return configured
    if has_llm and langgraph_available():
        return "agentic"
    return "simple"


def build_workflow(name: str) -> Workflow:
    """Instantiate a workflow, falling back to ``simple`` if unknown."""
    factory = get_workflow(name)
    if factory is None:
        available = ", ".join(entry["name"] for entry in list_workflows())
        logger.warning(
            "unknown assistant workflow %r; falling back to simple (have: %s)", name, available
        )
        from syncsage.assistant.workflows.simple import SimpleWorkflow

        return SimpleWorkflow()
    if hasattr(factory, "run") and not isinstance(factory, type):
        return factory  # already an instance
    return factory()


def reset_workflow_registry() -> None:
    """Drop programmatic registrations and the entry-point cache (tests)."""
    global _entry_point_cache
    _programmatic.clear()
    _entry_point_cache = None


__all__ = [
    "ENTRY_POINT_GROUP",
    "Workflow",
    "WorkflowRequest",
    "WorkflowResult",
    "WorkflowStep",
    "build_workflow",
    "get_workflow",
    "langgraph_available",
    "list_workflows",
    "register_workflow",
    "reset_workflow_registry",
    "resolve_workflow_name",
]
