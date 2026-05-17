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
docker compose --env-file .syncsage/compose.env up -d
```

`syncsage compose-env` renders the Docker Compose interpolation variables from the selected YAML. By default Compose mounts:

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

The GitHub Actions workflow at `.github/workflows/container.yml` validates pull requests, builds the root `Dockerfile`, checks the release version against existing GHCR tags, and pushes passing builds to GitHub Container Registry.

- Pull request to `main`: runs ruff correctness lint, dependency checks, source compilation, pytest on Python 3.11 and 3.12, package build, Docker Compose validation, Docker image build, and image smoke tests.
- Pull request release version: `pyproject.toml` must contain a stable semver version that is greater than the highest published GHCR semver image tag and has not already been published as either `#.#.#` or `v#.#.#`.
- Merged PR or direct patch to `main`: runs the same checks, then publishes `ghcr.io/esatt10/syncsage:<pyproject version>`, `ghcr.io/esatt10/syncsage:v<pyproject version>`, `ghcr.io/esatt10/syncsage:latest`, and `ghcr.io/esatt10/syncsage:sha-<commit>`.
- The workflow uses repository `GITHUB_TOKEN` permissions with `packages: write`.

For public local installs, make the package public from the GitHub package settings after the first image is published. To block merges without an incremented release version, require the container workflow status checks in branch protection for `main`.

## Version alignment

`pyproject.toml` is the single manual semver source. Run `python scripts/sync_version.py --bump patch`, `--bump minor`, `--bump major`, or `--set 1.2.3` to update it and refresh generated deployment defaults. CI runs `python scripts/sync_version.py --check` and rejects any version that is not greater than the highest published GHCR semver tag before publishing.

## Probes and ports

- API/UI port: `8765`
- Liveness: `GET /health`
- Readiness: `GET /ready`
- Metrics: `GET /metrics`

## Storage guidance

Do not share one writable `/state` volume across independent SyncSage instances. Use separate namespaces/PVCs or an explicit future coordination mode.
