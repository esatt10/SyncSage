# Deployment

SyncSage is packaged as one container image and can run locally, with Docker Compose, in Docker Desktop Kubernetes, or in enterprise Kubernetes.

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

```bash
cp syncsage.example.yaml syncsage.yaml
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d --build
```

This brings up two services: `syncsage` (API + MCP on `:8765`) and the optional
`syncsage-ui` sidecar (web UI on `:8080`). Run `docker compose up -d syncsage`
for a headless stack. `--build` matters for the UI: the sidecar has both a
`build:` context and an `image:` tag, so without it Compose keeps serving the
bundle it built the first time. Step-by-step UI instructions, including the
non-Docker path, live in [Run the web UI](how-to/run-the-ui.md).

`syncsage compose-env` renders the Docker Compose interpolation variables from the selected YAML. By default Compose mounts:

| Host value | Container path | Purpose |
|---|---|---|
| selected config path | `/config/syncsage.yaml` | Runtime config, read-only. |
| `deployment.compose.workspace_path` | `/workspace` | Indexed repositories and documents, read-only. |
| `deployment.compose.data_path` | `/data` | Extra local files that live **outside** the workspace, read-only. |
| `deployment.compose.vault_path` | `/vault` | Generated Obsidian notes, read/write. |
| `syncsage-state` volume | `/state` | SQLite, manifests, graph snapshots. |
| `syncsage-exports` volume | `/exports` | JSON/canvas exports. |

### Connecting local files that aren't in the workspace

A source can only index a path the container can actually see. **A host directory
that is not a subdirectory of the mounted workspace does not exist inside the
container** — registering a source for it fails with `path_missing` (the sync
result names the absent path and reminds you to mount it).

To index files that live somewhere else on the host, mount that directory in.
The compose file ships a ready-made second mount for exactly this:

```bash
# Index ~/research (outside ./workspace) — it appears in the container at /data:
SYNCSAGE_DATA_PATH="$HOME/research" docker compose up -d
# then register a source with path /data (already in security.allow_workspace_roots)
```

Need more than one extra directory? Add mounts in a `docker-compose.override.yml`
and add each *container* path to `security.allow_workspace_roots`:

```yaml
# docker-compose.override.yml
services:
  syncsage:
    volumes:
      - /abs/host/notes:/notes:ro
      - /abs/host/archive:/archive:ro
```

A relative source `path` (e.g. `path: docs`) is anchored to `workspace_root`, so
it means `/workspace/docs` — not a path relative to the container's working
directory. Use absolute container paths (`/data/...`, `/notes/...`) for anything
outside the workspace.

Check the API container:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

## MCP server inside Docker

For the primary VS Code workflow, start SyncSage with Compose and let VS Code attach to a foreground stdio MCP process inside the running container:

```bash
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

That command is normally launched by `.vscode/mcp.json`, not by hand. It must stay in the foreground because stdio is the transport.

To generate the VS Code config:

```bash
syncsage client-config vscode --output .vscode/mcp.json
```

If the local CLI is not installed, copy `examples/vscode/mcp.json` to `.vscode/mcp.json`.

For a one-off MCP-only container, use the generated Docker-run profile:

```bash
syncsage client-config vscode --mode docker-run --output .vscode/mcp.json
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
- Merged PR to `main`: publishes `ghcr.io/esatt10/syncsage:<pyproject version>` and, from the same commit, the web UI sidecar as `ghcr.io/esatt10/syncsage-ui:<pyproject version>` plus `:latest`. The shared version tag is what lets compose files pin the API and UI together. Direct pushes to `main` are not releaseable because there is no PR release-increment comment to read.
- The workflow uses repository `GITHUB_TOKEN` permissions with `packages: write`.

For public local installs, make the package public from the GitHub package settings after the first image is published — **both** packages, `syncsage` and `syncsage-ui`, or the UI sidecar fails to pull for anyone who is not authenticated to the registry. To block merges without validation and a checked release increment, require the CI checks and the `Release version selection` status in branch protection for `main`.

## Version alignment

`pyproject.toml` is the single source for the released semver. For local maintenance you can run `python scripts/sync_version.py --bump patch`, `--bump minor`, `--bump major`, or `--set 1.2.3` to update it and refresh generated deployment defaults. For PR releases, `patch` is selected by default; comment with a different release increment to override it and let the publish workflow update `main` before building the container.

## Probes and ports

- API/UI port: `8765`
- Liveness: `GET /health`
- Readiness: `GET /ready`
- Metrics: `GET /metrics`

## Storage guidance

Do not share one writable `/state` volume across independent SyncSage instances. Use separate namespaces/PVCs or an explicit future coordination mode.
