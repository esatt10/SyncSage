<p align="center">
  <img src="ui/public/pheasant.png" alt="" width="320">
</p>

# pheasant

Give pheasant the places where your useful context lives. It indexes them once,
keeps up as they change, and gives agents (or you) one place to search the lot.
Repositories, loose folders, Obsidian vaults, docs sites, images, audio: they all
end up in the same local knowledge graph, available over MCP, HTTP, and a web UI.

The important bit is that it stays yours. pheasant is local-first, does no LLM
work while indexing, and can run perfectly well without a router or a cloud
service. It is still an active prototype — I use it, I change it, and I would
rather say that plainly than dress it up as finished enterprise software.

pheasant is also the region half of
[Synapse](https://esatt10.github.io/pheasant-flock/). One container is one
self-searching "brain region"; [pheasant-flock](https://github.com/esatt10/pheasant-flock)
routes a question across as many regions as you choose to connect. That part is
entirely opt-in. A lone pheasant remains a useful pheasant. If you do want a
fleet, start with [Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md).

## What you get

- Incremental, repeatable sync. Unchanged files do not get pointlessly re-read.
- Text, graph, vector, and hybrid search over the same material.
- MCP tools for agents, an HTTP API, and a three-pane web UI for humans.
- Runtime source management without turning every small change into YAML work.
- A navigable Obsidian projection if that is where you prefer to think.
- Image captioning and audio transcription, with deterministic offline stubs by
  default and authored sidecars when you want exact text.
- State you can inspect and back up: SQLite, manifests, checkpoints, graph
  snapshots, and audit history all live under `/state`.

There is a proper [documentation site](#documentation) when you need the whole
reference. This README is here to get you oriented and running, not make you
read the manual twice.

## The shape of it

```text
YAML/profile config
  -> source registry
  -> connector-backed sync engine
  -> parsing and chunking
  -> graph enrichment
  -> text / graph / hybrid search
  -> MCP tools/resources, HTTP API, Obsidian projection
```

Under those arrows:

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

## Start here

`pheasant up` works out what you pointed it at — a folder,
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

If you want containers, `pheasant host` does the same detection, writes a
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

### If you want to see every knob

Do not hand-write a config from examples and hope it is current. Let the live
schema walk you through it:

```bash
pheasant setup            # sectioned interview; Enter accepts every default
pheasant setup --advanced # ask about every option
```

It explains each area before asking about it, reads its defaults off the live
schema so they are never stale, checkpoints your answers if you interrupt it,
and ends with a ready-to-run `pheasant.yaml`, a `0600` `.env` for any secrets
(only the env-var *name* ever reaches the YAML), and the startup commands for
your deployment target. See **[Set pheasant up](docs/how-to/setup.md)**.

If you really just want a starter file:

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

## How configuration works

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

## Keeping sources in sync

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

## Search, with structure

pheasant stores a directed multi-graph rather than flattening everything into a
bag of chunks. The main nodes are:

- `knowledge_base`, `source`, `file`, `document`, `markdown_note`, `chunk`
- `symbol`, `entity`, `concept`, `external_reference`

Core edges include:

- `contains`, `indexes`, `has_chunk`
- `mentions`, `derived_from`, `references`, `imports`, `calls`, `similar_to`

From the source material it derives:

- Python imports, classes, functions, constants, and call targets.
- Markdown/document headings, links, wiki links, URLs, citations, concepts, and named mentions.
- Lightweight cross-artifact similarity based on shared concepts.

Search uses that whole graph. `text` is SQLite full-text search, `graph` matches
nodes and relationships, and opt-in `vector` search handles semantic similarity.
`hybrid` is the default and combines whatever is available without returning
the same node three times. The practical payoff: symbols, concepts, entities,
and references can turn up even when no chunk contains the exact words you used.

You can ask questions too, rather than only search. `POST /assistant/chat`, the
`ask_knowledge_base` MCP tool, and the UI all use the same workflow: retrieve
evidence, pull in useful graph facts, and answer from those passages with
citations. Install `pheasant-kb[agent]` for the LangGraph workflow, or register
your own under `pheasant.agent_workflows`; the [workflow guide](docs/how-to/agent-workflows.md)
has the details. No model is involved in indexing, and if the answering model is
unavailable you still get the retrieved passages, citations, and facts.

See [docs/graph_model.md](docs/graph_model.md).

## Giving an agent access

Start MCP over stdio inside the running container:

```bash
docker exec -i pheasant python -m pheasant mcp --config /config/pheasant.yaml --transport stdio
```

Generate VS Code MCP config:

```bash
pheasant client-config vscode --output .vscode/mcp.json
```

The MCP surface is deliberately fairly boring: tools do one thing, and their
names say what that thing is. The ones you will use most are:

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

The usual runtime flow is:

1. Register a source through MCP.
2. Sync it.
3. Query it through search or graph tools.
4. Promote it to durable YAML config, or disable/remove it.
5. Inspect audit history through MCP resources/tools.

See [docs/mcp_tools.md](docs/mcp_tools.md) and [docs/mcp_client.md](docs/mcp_client.md).

## Taking the graph back to Obsidian

The graph does not have to stay hidden in a database. pheasant can project it
into a human-readable vault under `/vault/pheasant` by default.

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

## The HTTP side

These are the useful landmarks; the [HTTP reference](docs/reference/http-api.md)
has the complete list.

- `GET /health`, `GET /ready`, `GET /overview`
- `GET /sources`, `GET /sources/types`, `POST /sources`, `POST /sources/quick-add`
- `GET /sync/status`, `POST /sync`, `POST /sync/{source_id}`
- `POST /search`, `POST /relevant-files`
- `POST /assistant/chat`, `GET /assistant/workflows`, `POST /assistant/key`
- `GET /search/embeddings`, `PUT /search/embeddings`, `POST /search/embeddings/reindex`
- `GET /graph`, `GET /graph/export/node-link-json`, `GET /graph/export/cytoscape-json`
- `GET /mcp/info`
- `POST /obsidian/export`

## Web UI

A small React front end lives in [`ui/`](ui). Sources sit on the left, chat in
the middle, and the knowledge graph on the right. Ask something and the cited
nodes light up on the canvas beside the graph facts used in the answer. That is
more useful than another chat box that asks you to trust it.

It is not a cut-down demo. You can add sources, choose and tune an answering
workflow, configure semantic search, inspect MCP connection details, and edit
config as either a form or raw YAML with a diff before saving. Installed
connector plugins appear automatically because the UI asks the server what it
supports instead of baking that answer into the JavaScript bundle.

### Run it

Pick whichever route matches how you are already running pheasant. Full
instructions — including the easy-to-miss stale-image problem — are in
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

## Where it runs

Today that means:

- Local CLI process
- Docker
- Docker Compose
- Kubernetes manifests
- Helm chart skeleton

See [docs/deployment.md](docs/deployment.md).

## Working on pheasant

The normal checks are intentionally unsurprising:

```bash
python -m pytest
python -m ruff check src tests
python scripts/sync_version.py --check
```

## Documentation

The full documentation is a [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
site under [`docs/`](docs). Tutorials get you moving, how-to guides solve a
specific problem, and the reference is there when you really do need every
field. To read it locally:

```bash
pip install -e ".[docs]"
mkdocs serve          # live preview at http://localhost:8000
mkdocs build --strict # produce ./site
```

It is published to GitHub Pages by `.github/workflows/docs.yml` on pushes to `main`.

**Start here**

- [Documentation home](docs/index.md)
- Tutorials: [10-minute quickstart](docs/tutorials/quickstart.md) · [Multi-modal ingest](docs/tutorials/multimodal.md)
- How-to: [Set pheasant up](docs/how-to/setup.md) · [Run the web UI](docs/how-to/run-the-ui.md) · [Ask your knowledge base](docs/how-to/chat-and-ui.md) · [Customize the answering workflow](docs/how-to/agent-workflows.md) · [Configure sources](docs/how-to/sources.md) · [Vector self-search](docs/how-to/vector-search.md) · [Attach to a Synapse fleet](docs/how-to/attach-to-synapse.md) · [Backup & restore](docs/how-to/backup-restore.md)

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

## License, because there has to be a last section

pheasant is licensed under Apache-2.0. See [LICENSE](LICENSE).
