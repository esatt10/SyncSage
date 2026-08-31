<p align="center">
  <img src="ui/public/pheasant.png" alt="" width="320">
</p>

<h1 align="center">pheasant</h1>

<p align="center">
  <em>Context, memory and knowledge for you and your agents—in one container you run yourself.</em>
</p>

Pheasant is a local-first MCP knowledge server. Point it at repositories,
notes, documents or connected services and it builds searchable text, vectors
and a knowledge graph for people and agents.

## Start

```bash
docker run -p 127.0.0.1:8765:8765 \
  -v "$PWD:/workspace:ro" \
  -v pheasant-state:/state \
  ghcr.io/esatt10/pheasant
```

Open <http://127.0.0.1:8765>. The same address serves the UI, HTTP API and the
streamable HTTP MCP endpoint at `/mcp`.

That command needs no config file, database, broker or API key. Pheasant uses
SQLite and text search locally, writes its own initial configuration and
indexes `/workspace`.

## What it provides

- Hybrid text, vector and graph retrieval with source-level provenance.
- Grounded answers with citations through MCP, HTTP or the bundled UI.
- Durable agent memory stored as ordinary, searchable Markdown records.
- Incremental repository and document synchronization.
- A single image that can also use LanceDB, PostgreSQL, NATS, gRPC workers,
  WASM acceleration and agentic retrieval when enabled.

## A look at it

<p align="center">
  <img src="docs/assets/ui/notebook.png" alt="Searching the knowledge base, with results and their provenance" width="900">
</p>

Ask a question and get passages back with where each one came from. No API key
is connected here — pheasant answers extractively from the index, and cites
every passage. Connect a model and the same retrieval gets synthesized instead.
MCP, HTTP and the UI all run that one ranking.

<p align="center">
  <img src="docs/assets/ui/memory.png" alt="The Memory tab: a proposed memory expanded to its calls and spans, with a side panel resolving one returned key to the text behind it" width="900">
</p>

Memory is ordinary indexed Markdown, so recall is just search. The **Proposed**
section is [memory formation](docs/memory-formation.md): patterns the region
noticed in how it is actually used — here, that everyone searches for `router`
while the documents say `pheasant-flock`.

A proposal is reviewable, not just assertable. Open one and it shows the calls
it came from — what was asked, what came back, from which session — and opening
again shows the trace: each span, its timing, and the step where the rule
consolidated them into the proposal. Select a span or the criteria and a side
panel opens on that one call: the ids in full, the criteria the search ran
under, and the keys it returned — each of which resolves to the text it names,
so "why was this proposed" ends at real content rather than at a hash.

Proposals group by rule, filter by text, and promote or reject in bulk, because
a region under real traffic offers dozens at a time and the work is triage.
Nothing proposed is retrievable until someone promotes it, and a rejection is
permanent.

<p align="center">
  <img src="docs/assets/ui/graph.png" alt="The knowledge graph, showing files, symbols and the relationships between them" width="900">
</p>

<p align="center">
  <img src="docs/assets/ui/evaluation.png" alt="The Effectiveness page: a health vector where every tile carries its denominator, with one metric opened to the formula, the substituted numbers and its stated limitation" width="900">
</p>

**Is the knowledge base getting better, and how would we know?** That is a
different question from "did the sync work", and it has a page of its own.

There is deliberately no single accuracy score anywhere on it. What it publishes
is a *vector*: every tile carries the denominator it was computed over, and a
measurement that could not be made shows as a gap rather than as a zero —
because "we could not measure" and "we measured badly" are different findings,
and a dashboard that cannot tell them apart teaches people to ignore it. Click a
tile and it opens the formula, the numbers substituted into it, what was
excluded and why, and one sentence saying what the number does **not** support.

<p align="center">
  <img src="docs/assets/ui/evaluation-gates.png" alt="The hard gates: ACL leakage, stale-fact leakage, temporal correctness, abstention and control regression, each pass or fail with its evidence" width="900">
</p>

The hard gates sit apart from the scores, and not by accident. Metrics get
combined, and anything combined can be offset — an ACL leak paired with
excellent recall produces a healthy-looking composite. So gates are evaluated
*before* any aggregation, and they are rendered as a list rather than as tiles
on the same gradient. A gate that is not in force says so (`acl_leak` above,
on a region that has not enabled ACL enforcement) instead of failing every
default deployment.

<p align="center">
  <img src="docs/assets/ui/evaluation-running.png" alt="A batch in flight: a progress bar, the phase it is on, and 15 of 36 cohort/variant replays done" width="900">
</p>

A batch is minutes of work, so its progress is a **row in `/state`**, not an
object in the process running it. That is what lets this page show a run it did
not start — and keep showing it after the container that *was* running it is
restarted.

<p align="center">
  <img src="docs/assets/ui/evaluation-interrupted.png" alt="An interrupted batch: the process stopped after 15 of 36 replays, and running it again resumes from there" width="900">
</p>

And when that container stops, the page says so. Every finished
(cohort, variant) replay is checkpointed as it completes, so the next run picks
up where this one was cut off rather than starting over — never a spinner
nobody will ever stop.

Every screenshot above is generated by
[`scripts/screenshot_ui.py`](scripts/screenshot_ui.py) against a real region it
seeds and indexes — nothing is mocked for the camera. The proposals are
genuinely mined from the searches the script performs, and the effectiveness
numbers come from a real batch replaying those searches against a corpus-only
baseline, which is also why some of its tiles honestly read "not enough
evidence".

## Deployment profiles

Deployment files live under [`deploy/`](deploy/). Choose the smallest profile
that fits the workload:

| Profile | Use it for |
|---|---|
| [`local-small.yaml`](deploy/compose/local-small.yaml) | Offline/local SQLite and text search |
| [`local-advanced.yaml`](deploy/compose/local-advanced.yaml) | Single-node SQLite with LanceDB, OpenAI, WASM and agentic retrieval |
| [`fleet.yaml`](deploy/compose/fleet.yaml) | PostgreSQL, NATS and horizontally scaled gRPC workers |

Commands, required environment variables and operational notes are in the
[`deploy/compose` guide](deploy/compose/README.md). Local IDE agents can use the
repository’s [`pheasant-deploy` skill](.agents/skills/pheasant-deploy/SKILL.md)
to build from a blank canvas or select a preset.

## Configure

Do not hand-write `pheasant.yaml`. Use the live-schema setup flow:

```bash
pheasant setup
# or
pheasant setup --accept-defaults
```

Configuration details belong in the documentation:

- [Set Pheasant up](docs/how-to/setup.md)
- [Configuration reference](docs/configuration.md)
- [Configure sources](docs/how-to/sources.md)
- [Run the UI](docs/how-to/run-the-ui.md)
- [Attach a coding agent](docs/how-to/attach-to-coding-agent.md)
- [Monitor indexing](docs/how-to/monitor-indexing.md)
- [Scale a worker fleet](docs/how-to/worker-fleet.md)
- [Separate graph queries from API replicas](docs/how-to/graph-query-service.md)
- [Run the offline architecture regression](docs/how-to/architecture-regression.md)
- [Capacity planning](docs/how-to/capacity-planning.md)

## Develop

```bash
pip install -e ".[dev,mcp]"
pytest -q
ruff check src tests
```

Read [`CLAUDE.md`](CLAUDE.md) before changing the codebase. It contains the
architecture, invariants and canonical validation commands.

## License

Apache 2.0—see [LICENSE](LICENSE).
