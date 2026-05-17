# SyncSage

SyncSage is a lightweight, Docker-first MCP knowledge graph server for indexing local repositories, Markdown notes, documents, and Obsidian vaults. It keeps graph/search state fresh with startup validation, debounced file watching, scheduled fallback sync, and explicit agent-triggered refreshes.

## What SyncSage provides

- YAML-configured knowledge sources under allowlisted workspace roots.
- Persistent graph state, manifests, and SQLite/FTS search state.
- MCP tools/resources/prompts for low-token agentic retrieval.
- Optional Obsidian-compatible Markdown exports with stable note names.
- Local Docker, Docker Compose, Kubernetes, and Helm deployment examples.

## One-command local start

Prerequisites: Python 3.11+ and Docker Desktop or Docker Engine.

```bash
python scripts/bootstrap.py
```

The bootstrap is idempotent. It creates `syncsage.yaml` from `syncsage.example.yaml`
if needed, creates `.venv`, installs SyncSage with the MCP extra, renders
`.syncsage/compose.env`, creates the default workspace/vault folders, pulls the
configured GHCR image, starts Docker Compose, generates `.vscode/mcp.json`, runs an
initial sync, exports Obsidian notes, and prints the API, VS Code MCP, and Obsidian
validation paths.

If `uv` is installed, bootstrap uses it for the virtual environment and package
install. Otherwise it falls back to stdlib `venv` and `pip`.

By default the image is the semver tag declared in `syncsage.yaml`. To opt into
the moving GHCR `latest` tag:

```bash
python scripts/bootstrap.py --image ghcr.io/esatt10/syncsage:latest
```

To run source changes before they are published to GHCR, build the local checkout:

```bash
python scripts/bootstrap.py --build --image syncsage:local
```

`syncsage.yaml`, `.syncsage/compose.env`, `.vscode/mcp.json`, local state, and local vault output are ignored by git. Commit `syncsage.example.yaml` and files under `examples/` or `docs/` when you want to share a generalized setup.

## Docker Compose

Manual equivalent:

```bash
cp syncsage.example.yaml syncsage.yaml
python -m venv .venv
# Activate .venv, then:
python -m pip install -e ".[mcp]"
python -m syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env pull
docker compose --env-file .syncsage/compose.env up -d
```

The compose file gets its image and host mount values from the generated env file. Change those values under `deployment.compose` in the selected YAML.

## VS Code MCP Client

The primary client setup is VS Code connected to the MCP server running inside the SyncSage Docker container.

1. Start the container:

   ```bash
   python scripts/bootstrap.py
   ```

   Or, manually:

   ```bash
   python -m syncsage compose-env syncsage.yaml --output .syncsage/compose.env
   docker compose --env-file .syncsage/compose.env up -d
   ```

2. Generate or copy the VS Code MCP config:

   ```bash
   python -m syncsage client-config vscode --output .vscode/mcp.json
   ```

   If the local CLI is not installed, copy `examples/vscode/mcp.json` to `.vscode/mcp.json`.

3. In VS Code, run `MCP: List Servers`, start `syncsage`, and enable the tools in Agent mode.

The generated config uses `docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio`. Keep the compose container name as `syncsage`, or regenerate the config with `--container-name`.

For a one-off foreground MCP server without the API container:

```bash
python -m syncsage client-config vscode --mode docker-run --output .vscode/mcp.json
```

## Local CLI

After bootstrap, run the CLI from the local virtual environment:

```bash
.venv/bin/syncsage --help
```

On Windows PowerShell, use `.venv\Scripts\syncsage.exe` instead. To make the
shorter `syncsage` command available in your active shell, activate the virtual
environment or install the package in your user environment:

```bash
python -m pip install -e ".[mcp]"
```

See [docs/cli.md](docs/cli.md) for operator commands.

## Extending the Knowledge Base

Edit `syncsage.yaml` and add entries under `sources`. Host folders are mounted by
`deployment.compose.workspace_path`; source paths should use the container path
under `/workspace`.

```yaml
sources:
  - name: my-service
    type: repository
    path: /workspace/my-service
    include:
      - "**/*.py"
      - "**/*.md"
    exclude:
      - "**/.git/**"
      - "**/.venv/**"
      - "**/node_modules/**"
```

Place the repository at `./workspace/my-service`, rerun `python scripts/bootstrap.py`,
then trigger a full refresh if needed:

```bash
curl -X POST http://localhost:8765/sync -H "Content-Type: application/json" -d "{\"mode\":\"full\"}"
curl -X POST http://localhost:8765/obsidian/export
```

See [docs/configuration.md](docs/configuration.md) for source type examples and path rules.

## CI and container publishing

The repository keeps validation and publishing in separate workflows.

- `.github/workflows/ci.yml` runs ruff correctness lint, dependency checks, source compilation, pytest on Python 3.11 and 3.12, package build, Docker Compose validation, Docker image build, and image smoke tests.
- `.github/workflows/release-version.yml` runs from trusted base-branch code, comments on PRs with valid release increments, and defaults to `patch` / `3` unless a maintainer comments with `minor`, `major`, `2`, or `1`.
- `.github/workflows/container.yml` publishes only after CI passes on a push to `main`; it reads the merged PR release increment, bumps `pyproject.toml` and generated deployment tags on `main`, then builds the image.
- Merged PRs publish `ghcr.io/esatt10/syncsage:<pyproject version>` and update `ghcr.io/esatt10/syncsage:latest`. Direct pushes to `main` are not releaseable because there is no PR release-increment comment to read.
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
- `deployment`: local deployment helper values used to render Docker Compose env files.
- `sources`: repositories, Markdown folders, Obsidian vaults, document folders, web collections, or single files.

See [docs/configuration.md](docs/configuration.md) for details.

## MCP interface

The MCP server runs with:

```bash
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

The agent-facing tools are:

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

## Obsidian Workflow

SyncSage writes generated Markdown into the mounted `/vault` path, under `SyncSage/` by default. Open the host folder from `deployment.compose.vault_path` as an Obsidian vault. After indexing, call the MCP tool `export_obsidian_notes` or the API endpoint `POST /obsidian/export` to update the managed notes.

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
- [MCP client setup](docs/mcp_client.md)
- [CLI](docs/cli.md)
- [Deployment](docs/deployment.md)
- [Agentic workflows](docs/agentic_workflows.md)
- [Obsidian integration](docs/obsidian_integration.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)

## Development phases

The v0.1 MVP is complete when SyncSage can load config, index at least one repository plus Markdown/document folders, persist graph/search state, expose MCP retrieval/sync tools, re-index idempotently, detect file/git changes, export useful Obsidian notes, and run through Docker/Kubernetes examples.

## Versioning

`pyproject.toml` is the canonical semver source. To bump it and refresh generated deployment defaults:

```bash
python scripts/sync_version.py --bump patch
```

For PR releases, `patch` is selected by default. Comment with a different release increment to override it; each new comment reruns the release-version check against the current valid options. The publish workflow applies the selected increment to `main` before building the release image.

## License

License selection is pending. Apache-2.0 is recommended in the initial specification for permissive open-source distribution with an explicit patent grant.
