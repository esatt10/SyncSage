"""The single-pass workflow: retrieve once, answer once.

This is the default when no agent framework is installed and the fallback
whenever anything else is unavailable. It has no dependencies beyond the
standard library, so a question is always answerable — with a model it
synthesizes, without one it returns the retrieved passages attributed.

The prompt, citation numbering and fact collection are shared with the
agentic workflow (see :mod:`syncsage.assistant.chat`), so switching
workflows changes *how much work* is done, not what an answer looks like.
"""

from __future__ import annotations

from typing import Any

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


class SimpleWorkflow:
    """Retrieve once, synthesize once."""

    name = "simple"

    def run(self, request: WorkflowRequest, retriever: Any, llm: Any) -> WorkflowResult:
        passages = retriever.search(
            request.question,
            mode=request.mode,
            limit=request.max_results,
            source_name=request.source_name,
            principal=request.principal,
            principal_groups=request.principal_groups,
        )
        citations = passages_to_citations(passages, request.max_results)
        node_ids = [c["node_id"] for c in citations if c.get("node_id")]
        facts = retriever.facts(node_ids, int(request.options.get("max_facts", 12)))
        steps = [
            WorkflowStep(
                name="retrieve",
                detail=f"{request.mode} search for the question as asked",
                passages=len(citations),
            )
        ]
        # Progress is reported by every workflow, not just the agentic one:
        # a caller streaming the answer should not have to know which one ran.
        request.report(steps[-1])

        answer_mode = "extractive"
        error: str | None = None
        if llm is not None and citations:
            try:
                answer = llm.complete(
                    SYSTEM_PROMPT, build_prompt(request.question, citations, facts)
                )
                answer_mode = "llm"
                steps.append(WorkflowStep(name="answer", detail=f"synthesized with {llm.model_id}"))
                request.report(steps[-1])
            except ProviderError as exc:
                error = str(exc)
                answer = extractive_answer(request.question, citations, reason=short_reason(error))
        else:
            answer = extractive_answer(request.question, citations)

        mark_used_citations(answer, citations)
        return WorkflowResult(
            answer=answer,
            citations=citations,
            facts=facts,
            focus_node_ids=node_ids,
            mode=answer_mode,
            provider=llm.provider if llm else None,
            model=llm.model_id if llm else None,
            error=error,
            search_mode=request.mode,
            counts={"passages": len(passages), "citations": len(citations)},
            steps=steps,
            workflow=self.name,
        )
