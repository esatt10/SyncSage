# Customize the question-answering workflow

Every question — from the UI's chat panel, from `POST /assistant/chat`, or
from the MCP tool `ask_knowledge_base` — is answered by a **workflow**. A
workflow turns a question into a grounded answer using SyncSage's own search;
which one runs is configuration, and writing your own is a registration
rather than a fork.

SyncSage ships two.

| Workflow | What it does | Needs |
|---|---|---|
| `simple` | One retrieval pass, one model call. Predictable and fast. | nothing |
| `agentic` | A [LangGraph](https://langchain-ai.github.io/langgraph/) state graph that plans sub-queries, retrieves across every search mode, walks the knowledge graph, grades its own evidence, loops when it is thin, and verifies its citations. | `pip install 'syncsage[agent]'` |

```yaml
assistant:
  workflow: auto        # auto | simple | agentic | <your plugin>
```

`auto` (the default) resolves to `agentic` when the `[agent]` extra is
installed **and** a model is reachable, otherwise `simple`. A planner with no
model to plan with is pure overhead, so the fallback is deliberate — and it is
never an error: an unknown or failing workflow degrades to `simple` with the
reason attached to the answer rather than returning a 500.

!!! note "Still no LLM in the indexing path"
    Workflows are a query-time surface. Nothing here runs during a sync, so
    re-indexing unchanged content stays byte-for-byte deterministic. With no
    provider reachable both workflows still answer **extractively** — the top
    passages with citations and graph facts intact.

---

## The agentic graph

```
        ┌──────────────────────────── retry while evidence is thin
        ▼                                                        │
START → plan → retrieve → expand → grade ─── sufficient ──→ synthesize → verify → END
```

| Node | What it does |
|---|---|
| `plan` | Asks the model for 2–4 sub-queries and a search mode per query. Model-free or on a parse failure it falls back to the question verbatim — the graph never stalls on a bad plan. |
| `retrieve` | Fans the plan out over `hybrid`, `vector`, `text` and `graph`, merging by passage. Modes the deployment cannot serve (`vector` without embeddings) are dropped, not errored. |
| `expand` | Walks the knowledge graph out of the best hits, pulling in material lexical search would never surface. This is the step that makes a *graph* worth having. |
| `grade` | Asks the model whether the evidence answers the question. "No" sends it back to `plan` with what it learned, up to `max_rounds`. |
| `synthesize` | Writes the answer from the selected passages only, citing `[1]`, `[2]`, … |
| `verify` | Drops `[n]` markers that do not resolve to a real passage, so a hallucinated citation cannot reach the UI. |

Every step is recorded. The answer carries a `steps[]` trace (name, detail,
passage count), which the chat panel renders under the answer — so "why did it
say that" is inspectable rather than asserted.

### Tuning it

Options merge over the workflow's defaults. Set them globally:

```yaml
assistant:
  workflow: agentic
  workflow_options:
    agentic:
      max_rounds: 3
      retrieval_modes: [hybrid, vector, graph]
      expand_depth: 2
      max_context_passages: 14
```

…or per request, which wins:

```bash
curl -X POST http://localhost:8765/assistant/chat \
  -H 'content-type: application/json' \
  -d '{"question": "how do we charge customers",
       "workflow": "agentic",
       "options": {"max_rounds": 1, "grade_evidence": false}}'
```

| Option | Default | Effect |
|---|---|---|
| `max_rounds` | `2` | plan → retrieve → grade loops before answering with what it has. |
| `retrieval_modes` | `["hybrid", "vector"]` | Search modes to fan out over. Unavailable modes are dropped. |
| `expand_graph` | `true` | Walk the graph out of the best hits. |
| `expand_depth` | `1` | Hops the walk takes. |
| `expand_per_node` | `3` | Related documents pulled in per hit. |
| `per_query_results` | `6` | Passages fetched per query per mode. |
| `max_context_passages` | `10` | Passages offered to the answering step. |
| `grade_evidence` | `true` | Ask the model whether its evidence is sufficient. |
| `verify_citations` | `true` | Drop `[n]` markers with no matching passage. |
| `max_facts` | `12` | Graph facts surfaced alongside the answer. |

In the UI these are all under **Workflow** in the chat pane header; a
selection there applies to your next question only, so you can compare
behavior without editing config.

---

## Writing your own

A workflow implements exactly one method:

```python
from syncsage.assistant.workflows import WorkflowRequest, WorkflowResult, WorkflowStep

class CitationsOnlyWorkflow:
    name = "citations-only"

    def run(self, request: WorkflowRequest, retriever, llm) -> WorkflowResult:
        passages = retriever.search(request.question, mode="hybrid", limit=5)
        return WorkflowResult(
            answer="\n\n".join(p.snippet for p in passages),
            citations=[{"index": i + 1, "title": p.title, "node_id": p.node_id}
                       for i, p in enumerate(passages)],
            steps=[WorkflowStep("retrieve", "hybrid", len(passages))],
            workflow=self.name,
        )
```

`llm` may be `None` — a workflow **must** still return something useful in
that case. SyncSage is expected to work air-gapped, and the retrieval half of
an answer needs no model.

### The retriever

`retriever` is a `SyncSageRetriever`: the whole search surface, framework
agnostic, so a workflow written for LangGraph and one written for anything
else use the same toolbelt.

| Method | Returns |
|---|---|
| `search(query, mode=…, limit=…, source_name=…)` | `Passage`es from `text` / `graph` / `vector` / `hybrid`. |
| `multi_search(queries, modes=…)` | The above fanned out and merged deterministically by score then id. |
| `expand(passages, depth=…, per_node=…)` | Graph-walk neighbours of hits, as passages. |
| `neighbors(node_id, depth, edge_types)` / `slice(node_id, …)` | Raw graph navigation. |
| `facts(node_ids, limit)` | Subject–predicate–object triples around the cited nodes. |
| `content(node_id, max_chars)` | Full text behind a node. |
| `capabilities()` | Knowledge base, sources, available modes, whether vectors exist, node-type counts — and `as_prompt_context()` to put that in a system prompt. |

ACL enforcement (`security.acl_enforced`) is applied inside the retriever, so
a custom workflow inherits principal filtering without doing anything.

### Registering it

Programmatically, for an embedding application or a test:

```python
from syncsage.assistant.workflows import register_workflow
register_workflow("citations-only", CitationsOnlyWorkflow)
```

Or ship it in a package, exactly like the
[Connector SDK](../reference/connector-sdk.md):

```toml
[project.entry-points."syncsage.agent_workflows"]
citations-only = "my_pkg.flows:CitationsOnlyWorkflow"
```

Then select it anywhere a workflow name is accepted:

```yaml
assistant:
  workflow: citations-only
```

Installed plugins show up in `GET /assistant/workflows` and in the UI's
workflow picker automatically, marked as plugins.

### Overriding one node

You do not have to rewrite the agentic graph to change part of it. Its nodes
are a plain dict, and `AgenticWorkflow` takes overrides:

```python
from syncsage.assistant.workflows.agentic import AgenticWorkflow

def my_grade(state, ctx):
    return {"sufficient": len(state.get("passages", [])) >= 3}

register_workflow("agentic-cheap", lambda: AgenticWorkflow(nodes={"grade": my_grade}))
```

Each node is a function of `(state, ctx)` returning a state patch, so a
replacement is testable on its own without building a graph at all.

---

## Related

- [Ask your knowledge base (web UI)](chat-and-ui.md)
- [Vector self-search](vector-search.md) — gives the agent a second retrieval mode
- [Connector SDK](../reference/connector-sdk.md) — the same plugin pattern, for sources
