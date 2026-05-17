# Configuration

SyncSage reads YAML from `/config/syncsage.yaml` by default. Start from `syncsage.example.yaml` and mount it read-only into the container.

The local `syncsage.yaml` copy is intentionally ignored by git because it contains host-specific mount assumptions. Commit changes to `syncsage.example.yaml` when you want to update the shared pattern. Docker Compose env files generated under `.syncsage/` are ignored.

## Top-level sections

| Section | Purpose |
|---|---|
| `deployment` | Local deployment helper values, including Docker Compose image and host mount paths. |
| `syncsage` | Instance name, environment, workspace, state, vault, export paths, and logging. |
| `server` | API/MCP/UI host, port, OpenAPI, and transport settings. |
| `storage` | SQLite, graph, manifest, snapshot, compression, retention, and size limits. |
| `search` | Keyword, path, graph, hybrid ranking, and optional vector settings. |
| `sync` | Startup validation, watchers, git state checks, scheduler, idempotency, and concurrency. |
| `obsidian` | Optional Markdown/canvas export behavior. |
| `security` | Workspace allowlists, read-only mode, path traversal prevention, and secret excludes. |
| `sources` | Repository, Markdown, Obsidian, document, web, and single-file source definitions. |

## Source types

- `repository`: Git or non-Git source tree with optional dependency graph and git metadata.
- `markdown_folder`: Markdown files with headings, links, frontmatter, and tags.
- `obsidian_vault`: Markdown/canvas vault with `.obsidian` internals excluded.
- `document_folder`: PDF, DOCX, TXT, Markdown, HTML, and XML documents.
- `web_collection`: URL list or cached web artifacts for future optional ingestion.
- `single_file`: One local artifact.

## Extending a Local Knowledge Base

For Docker Compose, `deployment.compose.workspace_path` is the host folder mounted
read-only at `/workspace`. Every source path under `sources` should use the
container path, not the host path.

Default local mapping:

| Host path | Container path | Purpose |
|---|---|---|
| `./workspace` | `/workspace` | Repositories, notes, and documents to index. |
| `./vault` | `/vault` | Generated Obsidian output. |
| `syncsage-state` volume | `/state` | SQLite, manifests, graph snapshots. |

To add a repository:

```yaml
sources:
  - name: my-service
    type: repository
    path: /workspace/my-service
    description: Service repository used by the product team
    enabled: true
    include:
      - "**/*.py"
      - "**/*.md"
      - "**/*.yaml"
      - "**/*.toml"
    exclude:
      - "**/.git/**"
      - "**/.venv/**"
      - "**/node_modules/**"
      - "**/dist/**"
      - "**/build/**"
    repo:
      branch_policy: current
      include_uncommitted: true
      commit_trigger: true
    sync:
      on_startup: true
      on_file_change: debounce
      on_git_commit: true
      interval_seconds: 900
```

Place the repo at `./workspace/my-service`, or set
`deployment.compose.workspace_path` to the host directory that already contains
`my-service`. After changing YAML, rerun:

```bash
python scripts/bootstrap.py
```

To add a document folder:

```yaml
sources:
  - name: product-docs
    type: document_folder
    path: /workspace/product-docs
    include:
      - "**/*.pdf"
      - "**/*.docx"
      - "**/*.md"
      - "**/*.txt"
    exclude:
      - "**/~$*"
      - "**/.env*"
      - "**/*.pem"
      - "**/*.key"
```

To index an existing Obsidian vault as input, mount the host parent folder under
`/workspace` and add an `obsidian_vault` source. Keep generated SyncSage notes in
`/vault/SyncSage`; do not point an input source at that generated output unless
you intentionally want to index SyncSage's own notes.

Runtime MCP registration through `register_source` is useful for exploration, but
it reports `config_update_required: true`. Add stable sources to `syncsage.yaml`
so they survive container restarts.

## Include/exclude patterns

Use `include` to limit indexed content and `exclude` to avoid generated files, dependency folders, secrets, and large binaries. Keep these default exclusions unless a source requires otherwise:

```yaml
exclude:
  - "**/.git/**"
  - "**/.env"
  - "**/.env.*"
  - "**/*id_rsa*"
  - "**/*id_ed25519*"
  - "**/*.pem"
  - "**/*.key"
  - "**/node_modules/**"
  - "**/__pycache__/**"
  - "**/.venv/**"
  - "**/dist/**"
  - "**/build/**"
```

## Sync policies

- `on_startup: true` validates or repairs source state during cold start.
- `on_file_change: debounce` batches noisy filesystem events.
- `on_git_commit: true` refreshes repository context after detected HEAD changes.
- `interval_seconds` provides a scheduled fallback for missed watcher events.

## Safe path policy

All registered paths should resolve under configured allowlisted roots such as `/workspace` or `/vault`. Runtime source registration must reject path traversal and paths outside those roots.
