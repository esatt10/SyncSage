<p align="center">
  <img src="ui/public/pheasant.png" alt="" width="320">
</p>

<h1 align="center">pheasant</h1>

<p align="center">
  <em>Context, memory and knowledge for you and your agents — in one container you run yourself.</em>
</p>

AI Reasoning keeps getting better, but AI ready knowledge struggles to keep up. You already have
the knowledge — it's in a repo, a folder of notes, a stack of PDFs, an Obsidian
vault, a Notion workspace, a Slack channel you keep meaning to clean up. The hard
part was never having it. It's getting the right piece of it in front of an agent
at the moment that agent needs it, without pasting half your corpus into every
prompt and hoping, or building an overly complex harness that breaks down the next model update..

**pheasant is a Docker-first, local-first MCP context server.** Point it at what
you already have — folders, git repositories, PDFs and Office documents, Obsidian
vaults, images, audio, Notion, Google Drive, Slack, Confluence, IMAP — and it
turns them into a queryable **knowledge graph** that agents reach over MCP and
people reach over HTTP or the bundled UI.

It makes a knowledge base **easy to stand up, effective to query, and cheap to
keep** — for one person, a team, an organisation, and the agents working on their
behalf. It runs as a single container with nothing behind it, and it runs as a
Postgres-backed, vector-searching, queue-driven fleet across sharded regions. The
same image, the same code, the same stable IDs. What changes between those two is
configuration.

## Start here

```bash
docker run -p 8765:8765 -v "$PWD:/workspace:ro" -v pheasant-state:/state \
  ghcr.io/esatt10/pheasant
```

That is the whole setup. No config file, no database, no broker, no API key —
the container writes its own config, indexes `/workspace`, and serves the API,
the MCP endpoint and the web UI on one port.

Each component is a backend you can
select the day your corpus or your traffic asks for one, and none of them is a
rewrite — see
[Simple by default, serious when you need it](#simple-by-default-serious-when-you-need-it).

### Memory is part of the knowledge base, not a log beside it

Human knowledge is stateful. It grows, it gets corrected, and it is influenced by memories.
pheasant treats memories as part of the knowledge base, not the agent, to mimic this:

- **A memory is a file in your corpus.** Each record is one frontmatter Markdown
  file, indexed by the ordinary pipeline — so **recall is just search**. What
  comes back is what's relevant to the question in front of the agent, not the
  whole history poured into a context window.
- **Knowledge changes as it's used.** A correction **supersedes** rather than
  overwrites, validity is filtered at query time, and `as_of` deliberately
  brings the old belief back. Nothing is destroyed to record that something
  changed.
- **It's yours, in the open.** Records are plain files under `/state`, greppable
  and diffable, joined to the graph by `about` and `supersedes` edges. There's no
  separate memory store to keep in sync, and nothing to export if you move on.

### What it costs to run

The default install talks to nothing: SQLite for state, an FTS5 index for text,
files on disk. Embeddings, captioning, transcription and an answering model are
each optional and each keep an offline path — so the recurring cost of a
pheasant knowledge base is the machine it runs on.

Turning them on doesn't have to change that. Every one of them takes a
`base_url`, so each can point at a hosted API *or* at something you run yourself:
embeddings and multi-modal captioning/transcription against any OpenAI-spec
endpoint, answering against Anthropic, OpenAI, Gemini or your own gateway. You
choose where the compute lives; pheasant doesn't hold an opinion about it.

`pheasant scan` projects the rest before you commit anything. A 10,000-file,
200 MB corpus comes out at ~63,000 graph nodes, ~800 MB of state, a suggested
container memory of **0.5 GiB**, and **about 33 seconds** for the first full
index — after which a re-sync of unchanged content does no work at all.

---

## What it looks like

**Ask a question; see what the answer stands on.** Sources on the left,
conversation in the middle, and the graph on the right filtered to the nodes that
answer actually cited. This instance has no model connected — which is why the
answer is the retrieved passages themselves. Point it at a model and the same
evidence comes back synthesised into prose, with the same citations.

![The pheasant workspace: sources, a cited answer, and the graph nodes behind it](docs/assets/ui/notebook.png)

**The graph is the index, not a picture of it.** Every artifact, chunk, symbol,
heading, entity and memory record is a node with a stable, documented ID; hubs,
orphans and shortest paths are how you audit what a retrieval will walk through.

![The graph explorer, showing the knowledge base, its sources and the files under them](docs/assets/ui/graph.png)

**Memory you can read, and correct.** Everything an agent has asserted, by scope
and subject — with the record it replaced shown underneath, because a correction
supersedes rather than overwrites.

![The memory page, listing org- and user-scoped records including a superseded one](docs/assets/ui/memory.png)

**Sources stay boring on purpose.** Add a path, URL or glob and pheasant infers
the rest; each source syncs, promotes into `pheasant.yaml`, or
goes away, on its own schedule.

![The sources page, listing an indexed git repository and its memory source](docs/assets/ui/sources.png)

**Then hand it to an agent.** One command writes the MCP client config for
Claude Code, Cursor or VS Code; an attached agent gets the whole tool surface —
search, graph traversal, provenance, and memory it can write back to.

![The connect-an-agent dialog, showing the MCP client config and tool list](docs/assets/ui/connect-agent.png)

---

## What you get

| | |
|---|---|
| **Search that knows structure** | Text (BM25), vector and graph search fused by reciprocal rank; filter by source, path, node type or document section. |
| **A real knowledge graph** | Files, chunks, symbols, entities, headings and cross-source references — with a stable, documented ID grammar. |
| **Answers with citations** | Ask a question over HTTP, MCP or the UI and get a grounded answer that names the passages it used. |
| **Agent memory that belongs to the corpus** | Agents write facts back; recall is ordinary search, corrections supersede rather than overwrite, and scopes are isolated per principal. |
| **Deterministic indexing** | No model decides what your graph looks like. The same content produces the same graph, the same stable IDs and the same answer, every run. |
| **Every format as searchable text** | Seven document formats extract real text — PDF, `.docx`, `.pptx`, `.xlsx`, `.doc`, `.rtf`, `.epub` — and images are captioned and audio transcribed into the same space, offline by default. |
| **Incremental by default** | Content hashes and connector checkpoints mean a re-sync of unchanged content does no work. |
| **Scale when you need it** | One container, or an autoscaling fleet — same image, same code, opt-in backends. |

## Simple by default, serious when you need it

None of what follows is a roadmap or an integration you'd have to write. It is
all built, tested and shipped in the same image; what a fresh install changes is
only how much of it is switched on. The left column is what you get with nothing
installed and nothing configured. The right column is a config change — sometimes
plus an extra — the day you want it.

| | Default — no dependencies | On when you ask for it |
|---|---|---|
| **State** | SQLite under `/state`, one writer process | `storage.backend: postgres` lifts the single-writer ceiling and lets API replicas, an indexer and a worker fleet share one knowledge base. `pheasant migrate --to postgres` copies every table, rebuilds the full-text index for the target dialect, verifies row counts, and only then renames the SQLite file to `*.migrated` — never deletes it. Stable IDs carry over byte-identically, so nothing is re-indexed, and `tests/test_backend_parity.py` gates both dialects against the same gold set. |
| **Search** | BM25 over FTS5 and graph search, fused by reciprocal rank, with column weights and structural priors | `search.embeddings.enabled: true` adds a third arm: chunks are embedded at sync time and `hybrid` starts fusing vector candidates too. Embeddings come from any OpenAI-spec endpoint — hosted or self-hosted — or a deterministic offline `stub`. Vectors go to **LanceDB** (`[vector]`), the configured default and the one that holds up at scale, or a flat `numpy` file with no extra dependency. Enabling it later is not a re-index: **Build missing vectors** embeds what SQLite already holds without re-reading a source file. |
| **Answering** | Extractive and fully offline: top passages, citations and graph facts | Point `assistant.provider` at Anthropic, OpenAI, Gemini or your own gateway and the same evidence comes back as prose with the same citations. `[agent]` adds the agentic workflow — plan, retrieve, walk the graph out of the best hits, grade its own evidence, verify every citation resolves — with every knob typed, validated and editable from the UI. |
| **Ingest throughput** | In-process and already parallel — four sources and eight files at a time, on one container | A durable queue behind `sync.queue.enabled`: `backend: local` keeps it in this knowledge base's own database, so there is still nothing extra to run; `nats` moves it to JetStream (`[queue]`) for a fleet. `--role worker` replicas autoscale on `pheasant_index_queue_depth` and talk HTTP or gRPC (`[grpc]`), behind pooled connections, full-jitter retry, per-endpoint circuit breakers, deadline propagation and idempotency keys. Remote preparation is an optimisation: no arrangement of worker failures changes what a sync produces. |
| **Corpus size** | One index holds everything | `pheasant shard plan` packs whole sources into separate instances, and `pheasant scan` projects RAM, disk and time before you commit to either. |
| **Connector isolation** | First-party and third-party plugins run in-process | A source's `connector.runtime: sandboxed` (`[wasm]`) runs a third-party connector inside a wasmtime guest with deterministic fuel metering, a linear-memory cap and capability-scoped host fetch. A guest that declares an import the sandbox never wires fails to load at all. |
| **Analysis** | Search, graph traversal, provenance | `pheasant export parquet` writes artifacts, chunks, symbols, memory records and the whole graph to `/exports`, and DuckDB (`[analytics]`) runs SQL over it. The files outlive the container — pandas, polars and Spark read them with no pheasant process running. |
| **Federation** | One knowledge base, answering for itself | Fill in `synapse:` and the instance publishes a bounded semantic contract describing what it knows well, optionally Ed25519-signed (`[a2a]`), for a router to score and query. See [Roadmap](#roadmap-many-knowledge-bases-one-question). |

Two things hold across that whole table. Each opt-in is independent — Postgres
doesn't oblige you to run a broker, embeddings don't change where state lives,
and a fleet doesn't change what a sync produces. And the dependency-free side
isn't a trial mode you're expected to grow out of: every seam owes a test
asserting the no-infrastructure path is unchanged, and a standalone pheasant with
nothing configured is a supported deployment for as long as you want it to be.

## Install

```bash
pip install "pheasant-kb[mcp]"                # the MCP server
pip install "pheasant-kb[mcp,vector,agent]"   # + semantic search + agentic answering
```

Extras, each gated so a default install carries none of them and the offline
test suite needs none of them:

| Extra | Unlocks |
|---|---|
| `mcp` | The 26-tool MCP surface, over stdio or streamable HTTP |
| `vector` | LanceDB as the vector store, for semantic search at scale |
| `agent` | The agentic answering workflow: plan, retrieve, expand, grade, verify |
| `postgres` | Postgres state — the backend that lifts the single-writer ceiling |
| `queue` | NATS JetStream as the durable index work queue |
| `grpc` | gRPC transport to preparation workers |
| `wasm` | Fuel-metered sandboxed connectors, plus hot-loop acceleration |
| `analytics` | DuckDB, for SQL over the Parquet exports |
| `a2a` | Ed25519-signed Synapse contracts |
| `docs` | Building the documentation site |

**The published container image installs every one of them.** Anything in that
table is a config change against the image you already pulled — no rebuild, no
second image, no separate "enterprise" distribution.

## Get started

```bash
pheasant up ~/notes                                # a folder
pheasant up https://github.com/you/project         # cloned, then indexed
pheasant up ~/notes https://docs.example.com/guide # several at once
```

`up` detects what you pointed it at, writes a config, indexes it, and serves.
Three other front doors exist for when you want more control:

| Command | For |
|---|---|
| `pheasant setup` | An interactive wizard that explains each section and writes `pheasant.yaml` plus a `0600` `.env`. Every default is read off the live schema, so it can't go stale. |
| `pheasant host ~/notes` | Generates the config *and* a Compose file with your directories mounted, then runs it. |
| `pheasant init --profile team` | Just the starter config; edit it yourself. |

Then point an agent at it:

```bash
pheasant client-config claude-code   # emit MCP client config for your tool
```

## Use it

**From an agent (MCP).** 26 tools over stdio or streamable HTTP. The ones that
matter most: `search_context` (text/vector/graph/hybrid retrieval with criteria),
`ask_knowledge_base` (a grounded answer with citations), `get_relevant_files`
(what to read for a task), `get_graph_neighbors` and `explain_node` (walk the
graph), `memory_write` (remember something), `register_source` and `sync_source`
(manage the corpus at runtime), and `describe_retrieval` (what this instance is
configured to do).

**From anything else (HTTP).** `/search`, `/assistant/chat`, `/graph`,
`/sources`, `/sync`, `/memory`, `/jobs`, `/metrics`, `/health`, `/ready`.
Full reference: [HTTP API](docs/reference/http-api.md).

**From a browser.** The bundled UI is the three-pane research workspace shown
above, plus a full-screen graph explorer with hub, orphan and shortest-path
tools, a memory page, a config editor, and live indexing progress.

**From SQL.** `pheasant export parquet` writes the corpus — artifacts, chunks,
symbols, memory records and the graph as nodes and edges — to `/exports` as
Parquet, and `pheasant export query "SELECT …"` runs SQL over it. Search answers
"what is relevant to this question"; this answers "what is *in* this corpus",
with a `GROUP BY`. The files outlive the container: DuckDB, pandas, polars and
Spark read them with no pheasant process running. See
[Export a corpus as Parquet](docs/how-to/parquet-exports.md).

## Scaling

pheasant runs as one container until it shouldn't — and it carries more than
people expect before that point. Past it, three axes scale independently, and
turning on any one of them leaves the other defaults exactly where they were:

- **Request traffic** — `serve --role api` replicas behind an HPA. They serve
  reads and publish index work instead of running it.
- **Ingest throughput** — an `indexer` claiming from a durable queue, plus
  `worker` replicas autoscaled on `pheasant_index_queue_depth`.
- **Corpus size** — `pheasant shard plan` packs whole sources into separate
  instances, so no one index has to hold everything.

Selectable backends, dependency-free side first: SQLite **or** Postgres state;
in-process, a local table **or** NATS JetStream for the queue; HTTP **or** gRPC
to workers. Remote preparation is an optimisation — no arrangement of worker
failures changes what a sync produces.

Manifests for both shapes ship in [`deploy/`](deploy/); see
[Capacity planning](docs/how-to/capacity-planning.md) and
[Running a worker fleet](docs/how-to/worker-fleet.md).

## Roadmap: many knowledge bases, one question

One pheasant is one knowledge base. The interesting problem after that is
**routing across several** — your team's, your org's, the one that only holds
last year's incidents — without merging them into a single index nobody can
reason about, and without asking every one of them every time.

That's **Synapse**, and half of it already ships here: every instance can publish
a bounded **semantic contract** derived from its own content, describing what it
knows well. The other half — scoring those contracts, fanning a global query out
to the regions that can answer it, and merging what comes back — is the
[pheasant-flock](https://github.com/esatt10/pheasant-flock) router, and it's
where this project is going next.

None of it is required, and none of it is on. Every Synapse setting is off unless
you fill in the `synapse:` block, and a router-less pheasant behaves identically
— that's a standing guarantee, not a phase. See
[Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md).

## Development

```bash
pip install -e ".[dev,mcp]"
pytest -q
ruff check src tests && ruff format --check src tests
```

The suite is offline by design — no network, no model downloads, no API keys.
Tests that need a real Postgres or NATS skip with a message telling you how to
provide one.

Two conventions worth knowing before you change anything:

1. **Indexing never calls a model.** Determinism is a product guarantee. The only
   sanctioned network calls at sync time are the optional embedder, captioner and
   transcriber, and each keeps an offline stub.
2. **`/state` is user data.** Schema changes ship a one-shot idempotent migration
   that preserves the original.

`CLAUDE.md` is the dense contributor hand-off: architecture, invariants, and the
traps this codebase has already fallen into. Issues and PRs are welcome.

## Documentation

Full docs — tutorials, how-to guides, reference, explanation — build with
MkDocs Material:

```bash
pip install -e ".[docs]" && mkdocs build --strict
```

Start with [10-minute quickstart](docs/tutorials/quickstart.md),
[Configure sources](docs/how-to/sources.md),
[Turn on vector search](docs/how-to/vector-search.md),
[Attach to a coding agent](docs/how-to/attach-to-coding-agent.md), and the
[Architecture](docs/architecture.md) overview.

## License

Apache 2.0 — see [LICENSE](LICENSE).
