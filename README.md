# pheasant

pheasant is a local-first MCP context server that turns project sources into a queryable knowledge graph for agents and humans. It syncs configured repositories, folders, files, Obsidian vaults, web collections, and experimental API/S3 sources; enriches them into graph relationships; exposes retrieval and lifecycle operations through MCP and HTTP; and can project the result into a navigable Obsidian vault.

The project is still an active prototype, but the current architecture is intentionally shaped around production concerns: connector boundaries, idempotent sync, persistent checkpoints, graph-derived search, runtime source lifecycle management, and inspectable configuration.

> **Part of the [Synapse Suite](https://esatt10.github.io/pheasant-flock/):** pheasant is the *region* component of **Synapse** — a hyperfast federated knowledge-base system in which each pheasant container is a self-searching "brain region" that publishes a **semantic contract**, and the [pheasant-flock](https://github.com/esatt10/pheasant-flock) router (the "nervous system") routes global queries across regions. The **suite front door** (whole-system view) is the [Synapse Suite site](https://esatt10.github.io/pheasant-flock/); this repo is the region/KB half. Attaching to a fleet is opt-in and standalone-safe — see the consumer guide [Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md). Region-side spec: `docs/SYNAPSE_INTEGRATION.md`; system design lives in the pheasant-flock repo (`docs/SYNAPSE_ARCHITECTURE.md`).

> **📖 Documentation site:** Full consumer docs (tutorials, how-to guides, reference, explanation) are published as a [MkDocs Material](https://www.mkdocs.org/) site — see the [Documentation](#documentation) section below. Build locally with `pip install -e ".[docs]" && mkdocs build --strict`.

> **Multi-modal ingest:** pheasant indexes **images** (`.png/.jpg/.jpeg/.webp/.gif`, captioned) and **audio** (`.wav/.mp3/.m4a/.flac/.ogg`, transcribed) alongside text. Both ship with a deterministic **offline stub** by default (no API keys, no model downloads) and support authored `.caption.txt`/`.transcript.txt` sidecars. See [Multi-modal ingest](docs/how-to/multimodal-ingest.md).

## What pheasant Does

- Ingests local and connector-backed sources through a `SourceConnector` abstraction.
- Maintains SQLite search state, source manifests, connector checkpoints, graph snapshots, and audit history under `/state`.
- Builds an enriched graph with sources, artifacts, chunks, symbols, entities, concepts, external references, and cross-artifact relationships.
- Serves MCP tools/resources for source registration, sync, search, graph traversal, source lifecycle operations, and Obsidian export.
- Provides HTTP endpoints for health, readiness, source status, sync, search, graph export, and Obsidian export.
- Generates previewable Obsidian notes using workflow profiles for engineering, research, and project operations.
- Supports layered configuration: base defaults + profile + YAML + CLI overrides.

## Architecture at a Glance

```text
YAML/profile config
  -> source registry
  -> connector-backed sync engine
  -> parsing and chunking
  -> graph enrichment
  -> text / graph / hybrid search
  -> MCP tools/resources, HTTP API, Obsidian projection
```

Core components:

| Component | Role |
|---|---|
| Source registry | Tracks configured and runtime-registered sources, status, and lifecycle audit events. |
| Connectors | Normalize filesystem, web collection, API, and S3-style sources into list/read/checkpoint operations. |
| Sync engine | Runs `incremental`, `full`, `validate_only`, and `repair` modes with stable manifests and checkpoints. |
| Ingestion pipeline | Parses supported text/document artifacts into chunks with provenance. |
| Graph builder | Creates stable graph nodes/edges and applies code, document, and similarity enrichment. |
| Search store | Combines SQLite FTS/path search, graph-derived term expansion, and graph search over node/relationship attributes via `text`/`graph`/`hybrid` modes. |
| MCP server | Provides the primary agent interface for retrieval, sync, graph navigation, and source lifecycle operations. |
| Obsidian exporter | Creates previewable source, concept, file, and optional chunk notes with graph-driven links. |

## Quick Start

**One line, any target.** `pheasant up` detects what you point it at — a folder,
an Obsidian vault, a git repo (local or a URL it clones), a docs site, an S3
bucket, or a connector — writes a config, indexes it, and serves the API + MCP:

```bash
pheasant up ~/notes                                   # a folder
pheasant up https://github.com/you/project            # cloned, then indexed
pheasant up ~/notes https://docs.example.com/guide    # several at once
pheasant up '~/clients/*'                             # one source per subfolder
pheasant up ~/projects --split                        # same, without the glob
```

An existing `pheasant.yaml` is never overwritten, and re-running re-indexes
nothing that has not changed.

A private GitHub repo needs `GITHUB_TOKEN` (or `GH_TOKEN`) set — see
`.env.example` — or the clone fails with an authentication error; a public
repo needs nothing.

**One line to host it.** `pheasant host` does the same detection, then writes a
compose file (mounting each local source read-only at `/sources/<name>`) and
brings the stack up:

```bash
pheasant host ~/notes                    # config + compose + docker compose up -d
pheasant host ~/notes --print-only       # write the compose file, run it yourself
pheasant host ~/notes --no-ui --port 9000
```

Open <http://localhost:8080> for the web UI, or <http://localhost:8765> for the
API and MCP endpoint. Trouble getting the UI up, or seeing a stale one? →
**[Run the web UI](docs/how-to/run-the-ui.md)**.

### The longer way

Prefer to be walked through every option instead of hand-editing YAML?
Run the **[guided config wizard](docs/how-to/config-wizard.md)** with
your coding agent of choice (Claude Code, Copilot, Codex, Gemini CLI) —
it explains each setting, tracks your progress so you never lose your
place, and ends with a ready-to-run `pheasant.yaml` + `.env` + startup
commands.

Or generate a starter config yourself:

```bash
pheasant init --profile quickstart --output pheasant.yaml
```

Inspect the resolved config after profile/YAML/override layering:

```bash
pheasant config show --effective --profile quickstart --config pheasant.yaml
```

Validate paths and runtime readiness:

```bash
pheasant doctor --profile quickstart --config pheasant.yaml
```

Run locally:

```bash
pheasant start --profile quickstart --config pheasant.yaml
```

Or run through Docker Compose (`--build` keeps the UI sidecar current; set
`deployment.compose.workspace_path` in your config, or the generated env file
mounts an empty `./workspace` and nothing gets indexed):

```bash
pheasant compose-env pheasant.yaml --output .pheasant/compose.env
docker compose --env-file .pheasant/compose.env up -d --build
```

Health checks:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

Local `pheasant.yaml`, `.pheasant/compose.env`, `.vscode/mcp.json`, state, and vault output are ignored by git.

## Configuration Model

pheasant resolves config in this order:

```text
base defaults + profile + user YAML + CLI/env overrides
```

Built-in profiles:

- `quickstart`: local defaults with API and MCP enabled.
- `dev`: local developer defaults with debug logging and chunk-note export.
- `team`: shared-service defaults with HTTP/SSE-oriented MCP settings.
- `cloud-hybrid`: cloud/mounted-source defaults with research-style Obsidian output.

Common commands:

```bash
pheasant init --profile dev --output pheasant.yaml
pheasant config show --effective --profile dev --config pheasant.yaml --set server.port=9001
pheasant validate pheasant.yaml
pheasant doctor --profile dev --config pheasant.yaml
```

See [docs/configuration.md](docs/configuration.md).

## Source Sync

Configured sources are listed under `sources:` in YAML and can also be registered at runtime through MCP.

Supported source types:

- `repository`
- `markdown_folder`
- `obsidian_vault`
- `document_folder`
- `single_file`
- `web_collection`
- `api` experimental
- `s3` experimental

Sync modes:

| Mode | Behavior |
|---|---|
| `incremental` | Uses connector checkpoints and hashes to skip unchanged artifacts. |
| `full` | Rebuilds artifact, chunk, graph, manifest, and checkpoint state for a source. |
| `validate_only` | Checks connector health and readability without writing index artifacts or manifests. |
| `repair` | Rebuilds missing or invalid state based on manifests and database rows. |

Run sync:

```bash
pheasant sync --config pheasant.yaml --source pheasant-repo --mode incremental
pheasant sync --config pheasant.yaml --all --mode full
```

## Knowledge Graph and Search

pheasant stores a directed multi-graph. Core nodes include:

- `knowledge_base`, `source`, `file`, `document`, `markdown_note`, `chunk`
- `symbol`, `entity`, `concept`, `external_reference`

Core edges include:

- `contains`, `indexes`, `has_chunk`
- `mentions`, `derived_from`, `references`, `imports`, `calls`, `similar_to`

Enrichment passes extract:

- Python imports, classes, functions, constants, and call targets.
- Markdown/document headings, links, wiki links, URLs, citations, concepts, and named mentions.
- Lightweight cross-artifact similarity based on shared concepts.

Search spans the whole knowledge graph. Four modes are available: `text` (SQLite full-text over chunk content and paths), `graph` (matches node labels, types and attribute values plus relationship types/endpoints), `vector` (embedding similarity — opt-in, see [Vector self-search](docs/how-to/vector-search.md)), and `hybrid` (the default — merges and re-ranks every available signal, de-duplicating by node). This surfaces concepts, symbols, entities and references that never appear verbatim in chunk text, and helps relate files even when no single chunk contains every query term. Both the retrieval mode and the result count are adjustable. Graph traversal honors depth and optional edge filters.

**Asking, not just searching.** `POST /assistant/chat`, the MCP tool `ask_knowledge_base`, and the UI's chat pane all run the same *agent workflow* over that search surface: retrieve, cite the passages, surface graph facts around them, then have a model write the answer from those passages alone. The default workflow (with `pip install 'pheasant-kb[agent]'`) is a LangGraph state graph that plans sub-queries, fans out across modes, walks the graph for material lexical search missed, grades its own evidence and loops when it is thin, then verifies its citations. It is fully customizable, and third-party workflows register under the `pheasant.agent_workflows` entry-point group — see [Customize the answering workflow](docs/how-to/agent-workflows.md). **No LLM ever runs during indexing**; with no provider reachable, answers degrade to extractive (top passages, citations and facts intact) rather than failing.

See [docs/graph_model.md](docs/graph_model.md).

## MCP Interface

Start MCP over stdio inside the running container:

```bash
docker exec -i pheasant python -m pheasant mcp --config /config/pheasant.yaml --transport stdio
```

Generate VS Code MCP config:

```bash
pheasant client-config vscode --output .vscode/mcp.json
```

Primary MCP tools:

- `list_knowledge_bases`
- `register_source`
- `list_sources`
- `disable_source`
- `remove_source`
- `promote_runtime_source_to_config`
- `sync_source`
- `sync_all`
- `search_context`
- `get_relevant_files`
- `get_graph_neighbors`
- `get_file_summary`
- `get_repo_map`
- `explain_node`
- `export_obsidian_notes`
- `get_sync_status`
- `get_sync_history`

Runtime lifecycle flow:

1. Register a source through MCP.
2. Sync it.
3. Query it through search or graph tools.
4. Promote it to durable YAML config, or disable/remove it.
5. Inspect audit history through MCP resources/tools.

See [docs/mcp_tools.md](docs/mcp_tools.md) and [docs/mcp_client.md](docs/mcp_client.md).

## Obsidian Projection

pheasant can export a human-readable vault projection under `/vault/pheasant` by default.

Preview without writing:

```bash
curl -X POST http://localhost:8765/obsidian/export \
  -H "content-type: application/json" \
  -d '{"preview": true, "template_profile": "engineering"}'
```

Write notes:

```bash
curl -X POST http://localhost:8765/obsidian/export
```

Generated layout:

```text
pheasant/
  Index.md
  Sources/
  Concepts/
  Files/
  Chunks/        # optional
```

Template profiles:

- `engineering`
- `research`
- `project-ops`

The exporter creates source -> concept -> file -> chunk navigation when the indexed graph has enrichment terms and chunk notes are enabled.

See [docs/obsidian_integration.md](docs/obsidian_integration.md).

## HTTP API

Important endpoints:

- `GET /health`, `GET /ready`, `GET /overview`
- `GET /sources`, `GET /sources/types`, `POST /sources`, `POST /sources/quick-add`
- `GET /sync/status`, `POST /sync`, `POST /sync/{source_id}`
- `POST /search`, `POST /relevant-files`
- `POST /assistant/chat`, `GET /assistant/workflows`, `POST /assistant/key`
- `GET /search/embeddings`, `PUT /search/embeddings`, `POST /search/embeddings/reindex`
- `GET /graph`, `GET /graph/export/node-link-json`, `GET /graph/export/cytoscape-json`
- `GET /mcp/info`
- `POST /obsidian/export`

Full list: [docs/reference/http-api.md](docs/reference/http-api.md).

## Web UI

A light React front end lives in [`ui/`](ui). It is a separate workload that
talks to the pheasant HTTP API, so the indexing container is unchanged: a
three-pane workspace with sources on the left, chat in the middle, and the
knowledge graph on the right. Asking a question outlines the cited nodes on the
canvas and lists the graph facts behind the answer, so the reasoning and the
structure stay side by side.

The UI is meant to reach everything the API does, for low-code users and
developers alike: quick source setup from a single field *and* a form covering
the whole source schema (including installed connector plugins), the agent
workflow picker with its tuning options, semantic-search configuration with
coverage and a rebuild that never re-reads a source file, an MCP connection
panel, and a configuration editor (form + raw YAML + diff preview). What a
given deployment can offer is read from the server rather than baked into the
bundle. Routes are defined in `src/pheasant/api/app.py`.

### Running it

There are three supported ways to get the UI in front of you. Full
instructions, including how to be sure you are looking at the *current* bundle:
**[Run the web UI](docs/how-to/run-the-ui.md)**.

```bash
# 1. Containers, one line — API on :8765, UI on :8080
pheasant host ~/notes

# 2. This repo's reference stack — always pass --build, or Compose reuses
#    the UI image it built the first time and your changes never appear
docker compose up -d --build            # UI on :8080, API on :8765

# 3. No Docker: build the bundle once and pheasant serves it on its own port
cd ui && npm ci && npm run build && cd ..
pheasant start --config pheasant.yaml   # UI + API on :8765

# 4. Developing the UI itself: hot reload against a running pheasant
cd ui && npm install && npm run dev     # dev server on http://localhost:5173
```

The UI and pheasant images are published from the same commit under the same
version tag, so upgrading one upgrades the other. See
[ui/README.md](ui/README.md) for build variables and design notes.

## Deployment

Supported deployment paths:

- Local CLI process
- Docker
- Docker Compose
- Kubernetes manifests
- Helm chart skeleton

See [docs/deployment.md](docs/deployment.md).

## Development and Verification

Run tests:

```bash
python -m pytest
```

Run focused lint:

```bash
python -m ruff check src tests
```

Version references are synchronized from `pyproject.toml`:

```bash
python scripts/sync_version.py --check
```

## Documentation

The full documentation is a [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
site under [`docs/`](docs), organized Diátaxis-style (tutorials / how-to /
reference / explanation). Build and serve it locally:

```bash
pip install -e ".[docs]"
mkdocs serve          # live preview at http://localhost:8000
mkdocs build --strict # produce ./site
```

It is published to GitHub Pages by `.github/workflows/docs.yml` on pushes to `main`.

**Start here**

- [Documentation home](docs/index.md)
- Tutorials: [10-minute quickstart](docs/tutorials/quickstart.md) · [Multi-modal ingest](docs/tutorials/multimodal.md)
- How-to: [Build your config with the guided wizard](docs/how-to/config-wizard.md) · [Run the web UI](docs/how-to/run-the-ui.md) · [Ask your knowledge base](docs/how-to/chat-and-ui.md) · [Customize the answering workflow](docs/how-to/agent-workflows.md) · [Configure sources](docs/how-to/sources.md) · [Vector self-search](docs/how-to/vector-search.md) · [Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md) · [Backup & restore](docs/how-to/backup-restore.md)

**Reference & explanation**

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Interface matrix](docs/reference/interfaces.md)
- [HTTP API](docs/reference/http-api.md)
- [Graph model](docs/graph_model.md)
- [MCP tools](docs/mcp_tools.md)
- [MCP client setup](docs/mcp_client.md)
- [Deployment](docs/deployment.md)
- [Agentic workflows](docs/agentic_workflows.md)
- [Obsidian integration](docs/obsidian_integration.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [pheasant as a Synapse region](docs/SYNAPSE_INTEGRATION.md)

## License

pheasant is licensed under Apache-2.0. See [LICENSE](LICENSE).
