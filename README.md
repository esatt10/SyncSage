# SyncSage

SyncSage is a local-first MCP context server that turns project sources into a queryable knowledge graph for agents and humans. It syncs configured repositories, folders, files, Obsidian vaults, web collections, and experimental API/S3 sources; enriches them into graph relationships; exposes retrieval and lifecycle operations through MCP and HTTP; and can project the result into a navigable Obsidian vault.

The project is still an active prototype, but the current architecture is intentionally shaped around production concerns: connector boundaries, idempotent sync, persistent checkpoints, graph-derived search, runtime source lifecycle management, and inspectable configuration.

> **Part of the [Synapse Suite](https://esatt10.github.io/subjective-retrieval/):** SyncSage is the *region* component of **Synapse** — a hyperfast federated knowledge-base system in which each SyncSage container is a self-searching "brain region" that publishes a **semantic contract**, and the [subjective-retrieval](https://github.com/esatt10/subjective-retrieval) router (the "nervous system") routes global queries across regions. The **suite front door** (whole-system view) is the [Synapse Suite site](https://esatt10.github.io/subjective-retrieval/); this repo is the region/KB half. Attaching to a fleet is opt-in and standalone-safe — see the consumer guide [Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md). Region-side spec: `docs/SYNAPSE_INTEGRATION.md`; system design lives in the subjective-retrieval repo (`docs/SYNAPSE_ARCHITECTURE.md`).

> **📖 Documentation site:** Full consumer docs (tutorials, how-to guides, reference, explanation) are published as a [MkDocs Material](https://www.mkdocs.org/) site — see the [Documentation](#documentation) section below. Build locally with `pip install -e ".[docs]" && mkdocs build --strict`.

> **Multi-modal ingest:** SyncSage indexes **images** (`.png/.jpg/.jpeg/.webp/.gif`, captioned) and **audio** (`.wav/.mp3/.m4a/.flac/.ogg`, transcribed) alongside text. Both ship with a deterministic **offline stub** by default (no API keys, no model downloads) and support authored `.caption.txt`/`.transcript.txt` sidecars. See [Multi-modal ingest](docs/how-to/multimodal-ingest.md).

## What SyncSage Does

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

Generate a starter config:

```bash
syncsage init --profile quickstart --output syncsage.yaml
```

Inspect the resolved config after profile/YAML/override layering:

```bash
syncsage config show --effective --profile quickstart --config syncsage.yaml
```

Validate paths and runtime readiness:

```bash
syncsage doctor --profile quickstart --config syncsage.yaml
```

Run locally:

```bash
syncsage start --profile quickstart --config syncsage.yaml
```

Or run through Docker Compose:

```bash
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d
```

Health checks:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

Local `syncsage.yaml`, `.syncsage/compose.env`, `.vscode/mcp.json`, state, and vault output are ignored by git.

## Configuration Model

SyncSage resolves config in this order:

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
syncsage init --profile dev --output syncsage.yaml
syncsage config show --effective --profile dev --config syncsage.yaml --set server.port=9001
syncsage validate syncsage.yaml
syncsage doctor --profile dev --config syncsage.yaml
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
syncsage sync --config syncsage.yaml --source syncsage-repo --mode incremental
syncsage sync --config syncsage.yaml --all --mode full
```

## Knowledge Graph and Search

SyncSage stores a directed multi-graph. Core nodes include:

- `knowledge_base`, `source`, `file`, `document`, `markdown_note`, `chunk`
- `symbol`, `entity`, `concept`, `external_reference`

Core edges include:

- `contains`, `indexes`, `has_chunk`
- `mentions`, `derived_from`, `references`, `imports`, `calls`, `similar_to`

Enrichment passes extract:

- Python imports, classes, functions, constants, and call targets.
- Markdown/document headings, links, wiki links, URLs, citations, concepts, and named mentions.
- Lightweight cross-artifact similarity based on shared concepts.

Search spans the whole knowledge graph. Three modes are available: `text` (SQLite full-text over chunk content and paths), `graph` (matches node labels, types and attribute values plus relationship types/endpoints), and `hybrid` (the default — merges and re-ranks both, de-duplicating by node). This surfaces concepts, symbols, entities and references that never appear verbatim in chunk text, and helps relate files even when no single chunk contains every query term. Both the retrieval mode and the result count are adjustable. Graph traversal honors depth and optional edge filters.

See [docs/graph_model.md](docs/graph_model.md).

## MCP Interface

Start MCP over stdio inside the running container:

```bash
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

Generate VS Code MCP config:

```bash
syncsage client-config vscode --output .vscode/mcp.json
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

SyncSage can export a human-readable vault projection under `/vault/SyncSage` by default.

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
SyncSage/
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

- `GET /health`
- `GET /ready`
- `GET /sources`
- `GET /sync/status`
- `POST /sync`
- `POST /sync/{source_id}`
- `POST /search`
- `POST /relevant-files`
- `GET /graph`
- `GET /graph/export/node-link-json`
- `GET /graph/export/cytoscape-json`
- `POST /obsidian/export`

## Web UI

A light React front end lives in [`ui/`](ui). It is a separate workload that
talks to the SyncSage HTTP API, so the indexing container is unchanged. It
provides a Cytoscape knowledge-graph workspace (drill into sub-networks, filter
by edge type, inspect relationships and content, and search across nodes,
relationships and attributes with adjustable mode and result count), source
management with
add-a-local-directory registration, a full configuration editor (form + raw YAML
+ diff preview), and an Explain mode with a LaTeX-backed reference panel. The
underlying HTTP routes are defined in `src/syncsage/api/app.py` and described in
[docs/ui_recommendation.md](docs/ui_recommendation.md).

```bash
cd ui && npm install && npm run dev   # dev server on http://localhost:5173
```

See [ui/README.md](ui/README.md) for build and deployment options.

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
- How-to: [Configure sources](docs/how-to/sources.md) · [Vector self-search](docs/how-to/vector-search.md) · [Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md) · [Backup & restore](docs/how-to/backup-restore.md)

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
- [SyncSage as a Synapse region](docs/SYNAPSE_INTEGRATION.md)

## License

SyncSage is licensed under Apache-2.0. See [LICENSE](LICENSE).
