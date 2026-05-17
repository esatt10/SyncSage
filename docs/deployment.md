# Deployment

SyncSage is packaged as one container image and can run locally, with Docker Compose, in Docker Desktop Kubernetes, or in enterprise Kubernetes.

When running from a source checkout, use `python -m syncsage` from the repository root after dependencies are installed. The shorter `syncsage` command is available only after installing the package, for example with `python -m pip install -e ".[mcp]"`.

## Bootstrap Local Start

From a fresh clone with Python 3.11+ and Docker available:

```bash
python scripts/bootstrap.py
```

The command creates `syncsage.yaml` if needed, creates `.venv`, installs
`.[mcp]`, renders `.syncsage/compose.env`, creates the default host workspace and
vault folders, pulls the configured image from GHCR, starts Docker Compose,
generates `.vscode/mcp.json`, runs an initial sync/export, and prints validation
links and local paths.

Useful variants:

```bash
python scripts/bootstrap.py --skip-sync --skip-export
python scripts/bootstrap.py --skip-install --skip-pull
python scripts/bootstrap.py --image ghcr.io/esatt10/syncsage:latest
python scripts/bootstrap.py --build --image syncsage:local
python scripts/bootstrap.py --no-uv
```

If GNU Make is available, `make bootstrap` and `make start` wrap the same command.

## Local Docker

Create local config first:

```bash
cp syncsage.example.yaml syncsage.yaml
```

`syncsage.yaml` is ignored by git. Edit source paths so they point at container paths under `/workspace` or `/vault`, and edit `deployment.compose` if your host workspace or vault lives somewhere else.

```bash
docker run --rm \
  --name syncsage \
  -p 8765:8765 \
  -v "$PWD/syncsage.yaml:/config/syncsage.yaml:ro" \
  -v "$HOME/projects:/workspace:ro" \
  -v "$HOME/SyncSageVault:/vault" \
  -v syncsage-state:/state \
  ghcr.io/esatt10/syncsage:<pyproject-version>
```

## Docker Compose

The bootstrap is the preferred local path. The manual Compose flow, after
creating/activating a virtual environment and installing `.[mcp]`, is:

```bash
cp syncsage.example.yaml syncsage.yaml
python -m syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env pull
docker compose --env-file .syncsage/compose.env up -d
```

`python -m syncsage compose-env` renders the Docker Compose interpolation variables from the selected YAML. By default Compose mounts:

| Host value | Container path | Purpose |
|---|---|---|
| selected config path | `/config/syncsage.yaml` | Runtime config, read-only. |
| `deployment.compose.workspace_path` | `/workspace` | Indexed repositories and documents, read-only. |
| `deployment.compose.vault_path` | `/vault` | Generated Obsidian notes, read/write. |
| `syncsage-state` volume | `/state` | SQLite, manifests, graph snapshots. |
| `syncsage-exports` volume | `/exports` | JSON/canvas exports. |

Check the API container:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

## MCP server inside Docker

For the primary VS Code workflow, start SyncSage with Compose and let VS Code attach to a foreground stdio MCP process inside the running container:

```bash
python -m syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

That command is normally launched by `.vscode/mcp.json`, not by hand. It must stay in the foreground because stdio is the transport.

To generate the VS Code config:

```bash
python -m syncsage client-config vscode --output .vscode/mcp.json
```

If the local CLI is not installed, copy `examples/vscode/mcp.json` to `.vscode/mcp.json`.

For a one-off MCP-only container, use the generated Docker-run profile:

```bash
python -m syncsage client-config vscode --mode docker-run --output .vscode/mcp.json
```

## Kubernetes manifests

Apply the example namespace, ConfigMap, PVC, Deployment, and Service:

```bash
kubectl apply -f deploy/kubernetes/
```

The manifests assume one instance per namespace and one PVC-backed `/state` volume.

## Helm skeleton

```bash
helm template syncsage deploy/helm --namespace syncsage
helm install syncsage deploy/helm --namespace syncsage --create-namespace
```

The plain Kubernetes manifests and Helm defaults pin the current `pyproject.toml` version. Override the Helm image with `--set image.repository=... --set image.tag=...` when installing from a fork or a different release.

## CI and container registry

Validation and publishing are intentionally split across workflows.

- `.github/workflows/ci.yml`: runs ruff correctness lint, dependency checks, source compilation, pytest on Python 3.11 and 3.12, package build, Docker Compose validation, Docker image build, and image smoke tests.
- `.github/workflows/release-version.yml`: runs from trusted base-branch code, comments on PRs with valid release increments, and defaults to `patch` / `3` unless a maintainer comments with `minor`, `major`, `2`, or `1`.
- `.github/workflows/container.yml`: publishes after CI passes on a push to `main`; it reads the merged PR release increment, bumps `pyproject.toml` and generated deployment tags on `main`, then builds the image.
- Merged PR to `main`: publishes `ghcr.io/esatt10/syncsage:<pyproject version>` and updates `ghcr.io/esatt10/syncsage:latest`. Direct pushes to `main` are not releaseable because there is no PR release-increment comment to read.
- The workflow uses repository `GITHUB_TOKEN` permissions with `packages: write`.

For public local installs, make the package public from the GitHub package settings after the first image is published. To block merges without validation and a checked release increment, require the CI checks and the `Release version selection` status in branch protection for `main`.

## Version alignment

`pyproject.toml` is the single source for the released semver. For local maintenance you can run `python scripts/sync_version.py --bump patch`, `--bump minor`, `--bump major`, or `--set 1.2.3` to update it and refresh generated deployment defaults. For PR releases, `patch` is selected by default; comment with a different release increment to override it and let the publish workflow update `main` before building the container.

## Probes and ports

- API/UI port: `8765`
- Liveness: `GET /health`
- Readiness: `GET /ready`
- Metrics: `GET /metrics`

## Storage guidance

Do not share one writable `/state` volume across independent SyncSage instances. Use separate namespaces/PVCs or an explicit future coordination mode.
