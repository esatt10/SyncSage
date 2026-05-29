# Configuration

SyncSage reads YAML from `/config/syncsage.yaml` by default. Start from `syncsage.example.yaml` and mount it read-only into the container.

The local `syncsage.yaml` copy is intentionally ignored by git because it contains host-specific mount assumptions. Commit changes to `syncsage.example.yaml` when you want to update the shared pattern. Docker Compose env files generated under `.syncsage/` are ignored.

For a one-line local run, use a profile:

```bash
syncsage start --profile quickstart --config syncsage.yaml
```

Config is resolved as base defaults + profile + YAML + `--set` overrides. Inspect the result with:

```bash
syncsage config show --effective --profile dev --config syncsage.yaml
```

## How to use this guide

1. Copy `syncsage.example.yaml` to your runtime config path.
2. Keep the top-level sections in place and only edit values you need.
3. For each setting below, choose values based on your deployment mode and data sensitivity.
4. Validate by starting SyncSage and checking startup logs for loaded source counts and enabled transports.

---

## Top-level sections

| Section | Purpose | Required |
|---|---|---|
| `deployment` | Image and mount hints used by deployment tooling/templates. | Recommended |
| `syncsage` | Instance identity, environment label, and core filesystem roots. | Yes |
| `server` | API/MCP/UI network bindings and feature toggles. | Yes |
| `storage` | Database/graph/manifests locations and state limits. | Yes |
| `search` | Retrieval modes and ranking behavior. | Yes |
| `sync` | Watcher, git polling, schedule, idempotency, and concurrency behavior. | Yes |
| `obsidian` | Export controls for notes/canvas/frontmatter/backlinks/tags. | Optional |
| `security` | Path allowlisting and source-read protections. | Strongly recommended |
| `sources` | All indexed repositories/folders/files/URLs. | Yes |

---

## `deployment`

### `deployment.compose`

| Key | Type | Example | What it controls |
|---|---|---|---|
| `image_repository` | string | `ghcr.io/esatt10/syncsage` | Container image registry/repository for Compose-based runs. |
| `image_tag` | string | `0.1.3` | Image version tag used by deployment helpers. |
| `workspace_path` | path-like string | `./workspace` | Host path mounted to SyncSage `workspace_root`. |
| `vault_path` | path-like string | `./vault` | Host path mounted to SyncSage `vault_path`. |

---

## `syncsage` (core instance settings)

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | `local-syncsage` | Used as instance/knowledge-base identifier. |
| `description` | string | `Lightweight MCP knowledge graph and retrieval server` | Human-readable descriptor for operators. |
| `environment` | string | `local` | Label (for example `local`, `dev`, `staging`, `prod`). |
| `log_level` | string | `INFO` | Typical values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `state_path` | absolute path | `/state` | Base path for sqlite, graph snapshots, and manifests. |
| `vault_path` | absolute path | `/vault` | Output vault root for Obsidian exports. |
| `workspace_root` | absolute path | `/workspace` | Root path sources should live under. |
| `exports_path` | absolute path | `/exports` | Additional output path for generated artifacts. |

---

## `server` (connection options)

### Network binding

| Key | Type | Default | Notes |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address; set `127.0.0.1` for local-only exposure. |
| `port` | integer | `8765` | Primary service port. |

### MCP options (`server.mcp`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Global MCP enable/disable toggle. |
| `transports.stdio` | bool | `true` | Enables local stdio transport (common for editor integrations). |
| `transports.streamable_http` | bool | `true` | Enables HTTP streaming MCP transport. |
| `transports.sse` | bool | `false` | Enables SSE transport if your client requires it. |

### API/UI options

| Key | Type | Default | Notes |
|---|---|---|---|
| `api.enabled` | bool | `true` | Enables REST API endpoints. |
| `api.openapi` | bool | `true` | Exposes OpenAPI schema/docs endpoints. |
| `ui.enabled` | bool | `true` | Enables web UI routes (if packaged). |
| `ui.graph_visualization` | bool | `true` | Enables graph visualization features in UI. |

---

## `storage` (state + persistence)

| Key | Type | Default | Notes |
|---|---|---|---|
| `graph_format` | string | `node_link_json` | Graph serialization format. |
| `graph_snapshot_interval_seconds` | integer | `900` | Graph snapshot cadence. |
| `sqlite_path` | absolute path | `/state/syncsage.db` | Main SQLite database file. |
| `graph_path` | absolute path | `/state/graphs` | Directory for graph snapshots. |
| `manifest_path` | absolute path | `/state/manifests` | Directory for source manifests. Connector checkpoints are stored in SQLite. |
| `max_state_size_gb` | integer | `10` | Soft state budget for cleanup/policy logic. |
| `compression.enabled` | bool | `true` (example) | Optional compression toggle for persisted artifacts. |
| `compression.algorithm` | string | `zstd` (example) | Compression codec name. |
| `retention.keep_snapshots` | integer | `10` (example) | Snapshot retention count target. |
| `retention.keep_event_days` | integer | `30` (example) | Event retention age target in days. |

> Notes:
> - If `sqlite_path`, `graph_path`, or `manifest_path` are omitted, they are derived from `syncsage.state_path`.

---

## `search` (retrieval behavior)

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_mode` | string | `hybrid` | Typical modes: keyword, path, graph, hybrid (implementation-dependent). |
| `keyword.enabled` | bool | `true` (example) | Enables keyword index/query path. |
| `keyword.engine` | string | `sqlite_fts5` (example) | Keyword backend engine. |
| `embeddings.enabled` | bool | `false` (example) | Enables vector embedding generation/use. |
| `embeddings.provider` | string | `local` (example) | Embedding provider identifier. |
| `embeddings.model` | string | `sentence-transformers/all-MiniLM-L6-v2` (example) | Embedding model name. |
| `vector_store.enabled` | bool | `false` (example) | Enables vector store. |
| `vector_store.engine` | string | `chroma` (example) | Vector storage backend. |
| `vector_store.path` | absolute path | `/state/vector` (example) | Vector index persistence path. |
| `ranking.prefer_exact_path_matches` | bool | `true` (example) | Boost exact path matches. |
| `ranking.prefer_recent_commits` | bool | `true` (example) | Boost content tied to recent commits. |
| `ranking.graph_neighbor_boost` | bool | `true` (example) | Boost graph-adjacent matches. |
| `ranking.max_results_default` | integer | `10` | Default result count cap. |

---

## `sync` (orchestration + change detection)

### Startup policy

| Key | Type | Example | Notes |
|---|---|---|---|
| `startup.full_validation` | bool | `true` | Validate all sources on startup. |
| `startup.repair_missing_indexes` | bool | `true` | Rebuild missing index artifacts automatically. |

### Watcher policy (`sync.watcher`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Enable file-system watching. |
| `max_watch_paths` | integer | `100` | Upper bound on watched roots. |
| `debounce_ms` | integer | `1500` | Debounce delay before processing change bursts. |
| `batch_window_ms` | integer | `5000` | Batch window for event coalescing. |

### Git policy (`sync.git`)

| Key | Type | Default / Example | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Enable repository-aware sync behavior. |
| `detect_commit_changes` | bool | `true` | Detect HEAD updates. |
| `detect_branch_switch` | bool | `true` | Detect active-branch changes. |
| `reindex_on_commit` | bool | `true` | Re-index source content when commit changes are detected. |
| `reindex_on_branch_switch` | string | `validate_only` (example) | Branch-switch handling strategy. |

### Scheduler policy (`sync.scheduler`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Enable periodic fallback sync job. |
| `interval_seconds` | integer | `900` | Scheduler interval. |

### Idempotency + concurrency

| Key | Type | Example | Notes |
|---|---|---|---|
| `idempotency.hash_algorithm` | string | `sha256` | File identity hashing algorithm. |
| `idempotency.compare_size_mtime_hash` | bool | `true` | Use multiple file properties before reprocess. |
| `idempotency.skip_unchanged_files` | bool | `true` | Skip ingestion when source file is unchanged. |
| `concurrency.max_parallel_sources` | integer | `4` | Concurrent source processing cap. |
| `concurrency.max_parallel_files` | integer | `8` | Per-source file concurrency cap. |
| `concurrency.lock_timeout_seconds` | integer | `120` | Lock acquisition timeout. |

### Manual sync modes

| Mode | Behavior |
|---|---|
| `incremental` | Uses connector checkpoints and item/content hashes to skip unchanged artifacts. |
| `full` | Rebuilds artifact, chunk, graph, manifest, and checkpoint state for the selected source. |
| `validate_only` | Checks connector health and source readability without writing index artifacts or manifests. |
| `repair` | Rebuilds only missing or invalid artifact/chunk state detected from manifests and database rows. |

---

## `obsidian` (output options)

| Key | Type | Default / Example | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Master toggle for Obsidian exports. |
| `write_mode` | string | `upsert` | Export strategy (`upsert`, etc.). |
| `note_root` | string | `SyncSage` | Top-level folder/note namespace in the vault. |
| `template_profile` | string | `engineering` | One of `engineering`, `research`, or `project-ops`. |
| `create_index_notes` | bool | `true` | Generate index/navigation notes. |
| `create_source_notes` | bool | `true` | Generate one note per source. |
| `create_file_notes` | bool | `true` | Generate one note per file artifact. |
| `create_chunk_notes` | bool | `false` | Generate chunk-level notes. |
| `create_canvas` | bool | `true` | Generate canvas graph views. |
| `frontmatter.include_source_id` | bool | `true` | Include source identifier in note frontmatter. |
| `frontmatter.include_hash` | bool | `true` | Include content hash in frontmatter. |
| `frontmatter.include_last_indexed` | bool | `true` | Include index timestamp metadata. |
| `frontmatter.include_graph_node_id` | bool | `true` | Include graph node IDs in metadata. |
| `backlinks.enabled` | bool | `true` | Generate backlinks. |
| `backlinks.style` | string | `wikilink` | Backlink rendering style. |
| `tags.base` | list[string] | `[syncsage]` | Base tags appended to generated notes. |
| `tags.by_source_type` | bool | `true` | Add tags based on source type. |

---

## `security`

| Key | Type | Example | Notes |
|---|---|---|---|
| `allow_workspace_roots` | list[path] | `[/workspace, /vault]` | Allowed root prefixes for registered source paths. |
| `read_only_sources` | bool | `true` | Prevent source mutation operations. |
| `deny_path_traversal` | bool | `true` | Block `..` traversal and unsafe resolution. |
| `default_exclude_secrets` | bool | `true` | Apply secret-oriented default excludes. |

---

## `sources` (per-source configuration)

Each source item supports:

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | none | Unique source id/name. |
| `type` | enum | `single_file` | One of `repository`, `markdown_folder`, `obsidian_vault`, `document_folder`, `web_collection`, `single_file`, `s3`, `api`. |
| `path` | absolute path | none | Filesystem path for source root (or file). |
| `description` | string/null | null | Human-readable context for operators. |
| `enabled` | bool | `true` | Disable without deleting config. |
| `include` | list[glob] | code/text defaults | Inclusion patterns. |
| `exclude` | list[glob] | secure defaults | Exclusion patterns. |
| `repo.*` | object | see below | Repository-specific behavior. |
| `chunking.*` | object | see below | Chunking strategy/size overlap. |
| `sync.*` | object | see below | Source-specific trigger policies. |
| `connector.*` | object | see below | Connector feature flags and provider-specific options. |
| `urls` | list[string] | `[]` | URL list (mainly for `web_collection`). |

### `sources[].repo`

| Key | Type | Default | Notes |
|---|---|---|---|
| `branch_policy` | string | `current` | Branch selection policy for repository context. |
| `include_uncommitted` | bool | `true` | Include working tree changes. |
| `commit_trigger` | bool | `true` | Trigger sync on commit change events. |
| `dependency_graph` | object | `{}` | Optional language-specific dependency graph config. |

### `sources[].chunking`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | When false, each artifact is indexed as one full-content chunk. |
| `strategy` | string | `semantic` | Chunking algorithm (semantic/heading/page-oriented, etc.). |
| `max_chars` | integer | `4000` | Maximum chunk size. |
| `overlap_chars` | integer | `400` | Overlap between adjacent chunks. |

### `sources[].sync`

| Key | Type | Default | Notes |
|---|---|---|---|
| `on_startup` | bool | `true` | Process source at service start. |
| `on_file_change` | bool/string | `debounce` | File-change trigger behavior. |
| `on_git_commit` | bool | `true` | React to git commits for this source. |
| `interval_seconds` | int/null | `null` | Source-specific scheduled sync interval. |

### `sources[].connector`

Experimental non-filesystem connectors are disabled until explicitly enabled per source.

| Key | Type | Default | Notes |
|---|---|---|---|
| `allow_experimental` | bool | `false` | Required for `web_collection`, `api`, and `s3` connector execution. |
| `request_timeout_seconds` | integer | `10` | HTTP/API request timeout. |
| `headers` | map[string,string] | `{}` | Optional HTTP headers for web/API requests. |
| `api_endpoint` | string/null | `null` | JSON item listing endpoint for `api` sources. |
| `api_items_field` | string | `items` | JSON field containing API item records. |
| `api_content_field` | string | `content` | JSON field containing inline item content. |
| `s3_bucket` | string/null | `null` | Bucket name for `s3` sources. |
| `s3_prefix` | string | empty | Object prefix for `s3` sources. |

Example `web_collection` source:

```yaml
sources:
  - name: public-docs
    type: web_collection
    path: /workspace
    urls:
      - https://example.com/docs/overview.md
    connector:
      allow_experimental: true
    include:
      - "**/*.md"
```

---


## Release/version alignment (important for merges)

When a PR changes deployable server behavior and is merged, the release/version check expects a **new image version**. In practice:

- `pyproject.toml` version and deployment image tags must remain aligned for a release.
- `deployment.compose.image_tag` in `syncsage.example.yaml` is one of the generated references that should be incremented to the new server version during release prep.
- Use `python scripts/sync_version.py --check` in CI/local validation to confirm all generated version references are synchronized.

If this check fails on merge/release automation, bump the project version and re-run the sync script so config and deployment manifests match.

---

## Deployment modality examples

### 1) Local developer workstation (Docker Compose)

Use this for single-machine local development with mounted host directories.

```yaml
deployment:
  compose:
    image_repository: ghcr.io/esatt10/syncsage
    image_tag: 0.1.3
    workspace_path: ./workspace
    vault_path: ./vault

syncsage:
  environment: local
  log_level: INFO
  state_path: /state
  workspace_root: /workspace
  vault_path: /vault

server:
  host: 0.0.0.0
  port: 8765
  mcp:
    enabled: true
    transports:
      stdio: true
      streamable_http: true
      sse: false
```

### 2) Team shared VM / self-hosted service

Use this when multiple clients connect over network and you want stricter controls.

```yaml
syncsage:
  environment: prod
  log_level: INFO

server:
  host: 0.0.0.0
  port: 8765
  mcp:
    enabled: true
    transports:
      stdio: false
      streamable_http: true
      sse: true
  api:
    enabled: true
    openapi: false

security:
  allow_workspace_roots:
    - /workspace
    - /vault
    - /exports
  allow_user_selected_source_paths: true
  read_only_sources: true
  deny_path_traversal: true
  default_exclude_secrets: true
```

### 3) Obsidian-first personal knowledge vault

Use this when export quality to vault notes/canvas/backlinks is your top priority.

```yaml
obsidian:
  enabled: true
  write_mode: upsert
  note_root: SyncSage
  create_index_notes: true
  create_source_notes: true
  create_file_notes: true
  create_chunk_notes: false
  create_canvas: true
  frontmatter:
    include_source_id: true
    include_hash: true
    include_last_indexed: true
    include_graph_node_id: true
  backlinks:
    enabled: true
    style: wikilink
  tags:
    base:
      - syncsage
    by_source_type: true

sources:
  - name: existing-obsidian-vault
    type: obsidian_vault
    path: /workspace/obsidian-vault
    enabled: true
    include: ["**/*.md", "**/*.canvas"]
    exclude: ["**/.obsidian/**", "**/.trash/**"]
```

### 4) Repository indexing at scale (many files)

Use this when you ingest larger repositories and want predictable performance.

```yaml
sync:
  watcher:
    enabled: true
    max_watch_paths: 300
    debounce_ms: 2500
    batch_window_ms: 10000
  scheduler:
    enabled: true
    interval_seconds: 900
  concurrency:
    max_parallel_sources: 6
    max_parallel_files: 12
    lock_timeout_seconds: 180

sources:
  - name: primary-repository
    type: repository
    path: /workspace/repository
    include: ["**/*.py", "**/*.md", "**/*.yaml", "**/*.json"]
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
```

---

## Practical tuning checklist

- Start with the example file defaults.
- Use `syncsage init --profile <name>` to generate a focused starter config.
- Use `syncsage doctor --profile <name> --config syncsage.yaml` before long-running syncs.
- Confirm every `sources[].path` is under an allowed security root.
- Trim `include` patterns first; then harden `exclude` patterns.
- Keep `scheduler.interval_seconds` enabled as a safety net even when watcher is on.
- Enable vector settings only if you intend to run an embedding/vector stack.
- For production, disable OpenAPI/UI if not required.

