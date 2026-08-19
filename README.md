<p align="center">
  <img src="ui/public/pheasant.png" alt="" width="320">
</p>

<h1 align="center">pheasant</h1>

<p align="center">
  <em>Point it at your files. Get a knowledge graph your agents can search.</em>
</p>

pheasant is a Docker-first, local-first **MCP context server**. It indexes the
sources you configure — git repositories, folders, PDFs and Office documents,
Obsidian vaults, images, audio, Notion, Google Drive, Slack, Confluence, IMAP —
into a queryable **knowledge graph**, then serves retrieval over **MCP** and
**HTTP** with provenance on every result.

Indexing is deterministic: no model decides what your graph looks like. The
same content produces the same graph, the same stable IDs and the same answer,
every run.

```bash
docker run -p 8765:8765 -v "$PWD:/workspace:ro" -v pheasant-state:/state \
  ghcr.io/esatt10/pheasant
```

That is the whole setup. No config file, no database, no broker — the container
writes its own config, indexes `/workspace`, and serves the API, the MCP
endpoint and the web UI on one port.

---

## What you get

| | |
|---|---|
| **Search that knows structure** | Text (BM25), vector and graph search fused by reciprocal rank; filter by source, path, node type or document section. |
| **A real knowledge graph** | Files, chunks, symbols, entities, headings and cross-source references — with a stable, documented ID grammar. |
| **Answers with citations** | Ask a question over HTTP, MCP or the UI and get a grounded answer that names the passages it used. |
| **Agent memory** | Agents write facts back; recall is ordinary search, corrections supersede rather than overwrite, and scopes are isolated per principal. |
| **Incremental by default** | Content hashes and connector checkpoints mean a re-sync of unchanged content does no work. |
| **Scale when you need it** | One container, or an autoscaling fleet — same image, same code, opt-in backends. |

## Install

```bash
pip install "pheasant-kb[mcp]"        # the MCP server
pip install "pheasant-kb[mcp,vector,agent]"   # + semantic search + agentic answering
```

Optional extras, each gated so a default install carries none of them:
`mcp`, `vector` (LanceDB), `agent` (LangGraph answering), `a2a` (signed
contracts), `wasm` (sandboxed connectors), `postgres`, `grpc`, `queue` (NATS),
`docs`. The published container image installs all of them.

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
| `pheasant setup` | An interactive wizard that explains each section and writes `pheasant.yaml` plus a `0600` `.env`. Every default is read off the live schema, so it cannot go stale. |
| `pheasant host ~/notes` | Generates the config *and* a Compose file with your directories mounted, then runs it. |
| `pheasant init --profile team` | Just the starter config; edit it yourself. |

Then point an agent at it:

```bash
pheasant client-config claude-code   # emit MCP client config for your tool
```

## Use it

**From an agent (MCP).** 26 tools over stdio or streamable HTTP. The ones that
matter most: `search_context` (text/vector/graph/hybrid retrieval with
criteria), `ask_knowledge_base` (a grounded answer with citations),
`get_relevant_files` (what to read for a task), `get_graph_neighbors` and
`explain_node` (walk the graph), `memory_write` (remember something),
`register_source` and `sync_source` (manage the corpus at runtime), and
`describe_retrieval` (what this instance is configured to do).

**From anything else (HTTP).** `/search`, `/assistant/chat`, `/graph`,
`/sources`, `/sync`, `/memory`, `/jobs`, `/metrics`, `/health`, `/ready`.
Full reference: [HTTP API](docs/reference/http-api.md).

**From a browser.** The bundled UI is a three-pane research workspace — sources
on the left, chat in the middle, and the graph lighting up the nodes each answer
cited. Also a full-screen graph explorer with hub, orphan and shortest-path
tools, a config editor, and live indexing progress.

**From SQL.** `pheasant export parquet` writes the corpus — artifacts, chunks,
symbols, memory records and the graph as nodes and edges — to `/exports` as
Parquet, and `pheasant export query "SELECT …"` runs SQL over it. Search
answers "what is relevant to this question"; this answers "what is *in* this
corpus", with a `GROUP BY`. The files outlive the region: DuckDB, pandas,
polars and Spark read them with no pheasant process running. See
[Export a corpus as Parquet](docs/how-to/parquet-exports.md).

## Scaling

pheasant runs as one container until it shouldn't. Past that, three axes scale
independently — and every default stays exactly where it was:

- **Request traffic** — `serve --role api` replicas behind an HPA. They serve
  reads and publish index work instead of running it.
- **Ingest throughput** — an `indexer` claiming from a durable queue, plus
  `worker` replicas autoscaled on `pheasant_index_queue_depth`.
- **Corpus size** — `pheasant shard plan` splits whole sources across regions
  and the router fans out.

Selectable backends, dependency-free side first: SQLite **or** Postgres state;
in-process, a local table **or** NATS JetStream for the queue; HTTP **or** gRPC
to workers. `pheasant scan` projects RAM, disk and index time before you commit
to any of it.

Manifests for both shapes ship in [`deploy/`](deploy/); see
[Capacity planning](docs/how-to/capacity-planning.md) and
[Running a worker fleet](docs/how-to/worker-fleet.md).

## Part of Synapse

pheasant is also the **region** component of
[Synapse](https://esatt10.github.io/pheasant-flock/): each container publishes a
bounded **semantic contract** describing what it knows, and the
[pheasant-flock](https://github.com/esatt10/pheasant-flock) router scores those
contracts to decide which regions to ask.

This is entirely opt-in. Every Synapse setting is off unless you fill in the
`synapse:` block, and a router-less pheasant behaves identically. See
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

1. **Indexing never calls a model.** Determinism is a product guarantee. The
   only sanctioned network calls at sync time are the optional embedder,
   captioner and transcriber, and each keeps an offline stub.
2. **`/state` is user data.** Schema changes ship a one-shot idempotent
   migration that preserves the original.

`CLAUDE.md` is the dense contributor hand-off: architecture, invariants, and
the traps this codebase has already fallen into.

## Documentation

Full docs — tutorials, how-to guides, reference, explanation — build with
MkDocs Material:

```bash
pip install -e ".[docs]" && mkdocs build --strict
```

Start with [10-minute quickstart](docs/tutorials/quickstart.md),
[Configure sources](docs/how-to/sources.md),
[Attach to a coding agent](docs/how-to/attach-to-coding-agent.md), and the
[Architecture](docs/architecture.md) overview.

## License

Apache 2.0 — see [LICENSE](LICENSE).
