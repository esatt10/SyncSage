# SyncSage

SyncSage is a lightweight, Docker-first MCP knowledge graph server for indexing local repositories, Markdown notes, documents, and Obsidian vaults. It keeps graph/search state fresh with startup validation, debounced file watching, scheduled fallback sync, and explicit agent-triggered refreshes.

## What SyncSage provides

- YAML-configured knowledge sources under allowlisted workspace roots.
- Persistent graph state, manifests, and SQLite/FTS search state.
- MCP tools/resources/prompts for low-token agentic retrieval.
- Optional Obsidian-compatible Markdown exports with stable note names.
- Local Docker, Docker Compose, Kubernetes, and Helm deployment examples.

## Quick start with Docker

1. Copy the example configuration:

   ```bash
   cp syncsage.example.yaml syncsage.yaml
   ```

2. Edit source paths in `syncsage.yaml` so they match folders mounted into `/workspace`, `/vault`, and `/state`.

3. Run the container:

   ```bash
   docker run --rm \
     --name syncsage \
     -p 8765:8765 \
     -v "$PWD/syncsage.yaml:/config/syncsage.yaml:ro" \
     -v "$HOME/projects:/workspace" \
     -v "$HOME/SyncSageVault:/vault" \
     -v syncsage-state:/state \
     ghcr.io/esatt10/syncsage:latest
   ```

4. Check health endpoints:

   ```bash
   curl http://localhost:8765/health
   curl http://localhost:8765/ready
   ```

## Docker Compose

```bash
cp syncsage.example.yaml syncsage.yaml
docker compose up
```

The compose file mounts `./syncsage.yaml` into `/config/syncsage.yaml`, `~/projects` into `/workspace`, and `~/SyncSageVault` into `/vault`.

## CI and container publishing

The repository validates pull requests and publishes the Docker image with `.github/workflows/container.yml`. Release labeling is checked separately by `.github/workflows/release-tag.yml`.

- Pull requests to `main` run ruff correctness lint, dependency checks, source compilation, pytest on Python 3.11 and 3.12, package build, Docker Compose validation, Docker image build, and image smoke tests.
- Pull requests must have exactly one release label matching `#.#.#`, such as `1.2.3`; labels like `v1.2.3` are rejected. Adding or removing labels reruns only the lightweight release-label workflow.
- Merged PRs and direct patches to `main` run the same checks, then publish `ghcr.io/esatt10/syncsage:latest` and a `sha-<commit>` tag.
- The workflow uses `GITHUB_TOKEN` with `packages: write`; no separate registry secret is required for this repository.

After the first workflow run, set the package visibility in GitHub Packages if the image should be publicly pullable without authentication.

## Configuration overview

SyncSage reads `/config/syncsage.yaml` by default. The example file includes these main sections:

- `syncsage`: instance name, environment, paths, and logging.
- `server`: API/MCP/UI binding and transport settings.
- `storage`: SQLite, graph, manifest, snapshot, and retention settings.
- `search`: keyword, path, graph, hybrid, optional embeddings, and ranking settings.
- `sync`: startup validation, watcher, git monitor, scheduler, idempotency, and concurrency settings.
- `obsidian`: optional vault note and canvas export settings.
- `sources`: repositories, Markdown folders, Obsidian vaults, document folders, web collections, or single files.

See [docs/configuration.md](docs/configuration.md) for details.

## MCP interface

The intended agent-facing tools are:

- `list_knowledge_bases`
- `register_source`
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

Every retrieval response should include provenance such as source ID, path, branch/commit when available, timestamps, and reason/confidence metadata. See [docs/mcp_tools.md](docs/mcp_tools.md).

## Deployment paths

- **Local Docker:** fastest single-instance setup.
- **Docker Compose:** repeatable local setup with named state volume.
- **Docker Desktop Kubernetes:** local namespace/PVC simulation.
- **Enterprise Kubernetes:** isolated namespace per team/project, PVC-backed state, service probes, and optional ingress/network policy.
- **Helm:** skeleton chart under `deploy/helm` for parameterized Kubernetes installs.

See [docs/deployment.md](docs/deployment.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Graph model](docs/graph_model.md)
- [MCP tools](docs/mcp_tools.md)
- [Deployment](docs/deployment.md)
- [Agentic workflows](docs/agentic_workflows.md)
- [Obsidian integration](docs/obsidian_integration.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## Development phases

The v0.1 MVP is complete when SyncSage can load config, index at least one repository plus Markdown/document folders, persist graph/search state, expose MCP retrieval/sync tools, re-index idempotently, detect file/git changes, export useful Obsidian notes, and run through Docker/Kubernetes examples.

## License

License selection is pending. Apache-2.0 is recommended in the initial specification for permissive open-source distribution with an explicit patent grant.
