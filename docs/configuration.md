# Configuration

SyncSage reads YAML from `/config/syncsage.yaml` by default. Start from `syncsage.example.yaml` and mount it read-only into the container.

The local `syncsage.yaml` copy is intentionally ignored by git because it contains host-specific mount assumptions. Commit changes to `syncsage.example.yaml` when you want to update the shared pattern. Commit `.env.example`, but not `.env`.

## Top-level sections

| Section | Purpose |
|---|---|
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
