# Deployment

SyncSage is packaged as one container image and can run locally, with Docker Compose, in Docker Desktop Kubernetes, or in enterprise Kubernetes.

## Local Docker

Create local config first:

```bash
cp syncsage.example.yaml syncsage.yaml
```

`syncsage.yaml` is ignored by git. Edit source paths so they point at container paths under `/workspace` or `/vault`.

```bash
docker run --rm \
  --name syncsage \
  -p 8765:8765 \
  -v "$PWD/syncsage.yaml:/config/syncsage.yaml:ro" \
  -v "$HOME/projects:/workspace:ro" \
  -v "$HOME/SyncSageVault:/vault" \
  -v syncsage-state:/state \
  ghcr.io/esatt10/syncsage:latest
```

## Docker Compose

```bash
cp syncsage.example.yaml syncsage.yaml
cp .env.example .env
docker compose up -d
```

By default Compose mounts:

| Host value | Container path | Purpose |
|---|---|---|
| `SYNCSAGE_CONFIG_PATH=./syncsage.yaml` | `/config/syncsage.yaml` | Runtime config, read-only. |
| `SYNCSAGE_WORKSPACE_PATH=./workspace` | `/workspace` | Indexed repositories and documents, read-only. |
| `SYNCSAGE_VAULT_PATH=./vault` | `/vault` | Generated Obsidian notes, read/write. |
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
docker compose up -d
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

The plain Kubernetes manifests and Helm defaults both pull `ghcr.io/esatt10/syncsage:latest`.
Override the Helm image with `--set image.repository=... --set image.tag=...` when installing from a fork or a pinned release.

## Container registry

The GitHub Actions workflow at `.github/workflows/container.yml` builds the root `Dockerfile` and pushes to GitHub Container Registry.

- Push to `main`: publishes `ghcr.io/esatt10/syncsage:latest` and `ghcr.io/esatt10/syncsage:sha-<commit>`.
- Push a version tag such as `v0.1.0`: publishes `ghcr.io/esatt10/syncsage:v0.1.0`.
- The workflow uses repository `GITHUB_TOKEN` permissions with `packages: write`.

For public local installs, make the package public from the GitHub package settings after the first image is published.

## Probes and ports

- API/UI port: `8765`
- Liveness: `GET /health`
- Readiness: `GET /ready`
- Metrics: `GET /metrics`

## Storage guidance

Do not share one writable `/state` volume across independent SyncSage instances. Use separate namespaces/PVCs or an explicit future coordination mode.
