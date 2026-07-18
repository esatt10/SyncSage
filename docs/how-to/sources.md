# How to configure sources

Sources are the inputs SyncSage indexes. They live under `sources:` in your
`syncsage.yaml` and can also be registered at runtime over MCP
(`register_source`) and later promoted to durable config
(`promote_runtime_source_to_config`).

## Source types

| Type | Indexes |
|---|---|
| `repository` | A git repository (branch/commit-aware, dependency graph) |
| `markdown_folder` | A folder of Markdown notes |
| `obsidian_vault` | An existing Obsidian vault (`.md` + `.canvas`) |
| `document_folder` | PDFs, DOCX, TXT, HTML, XML |
| `single_file` | One file |
| `web_collection` | A set of web URLs |
| `memory` | Agent-memory records (see [Agent memory](agent-memory.md)) |
| `notion` | A Notion workspace, via an integration token (below) |
| `gdrive` | Google Drive docs + text files (`connector.api_key_env`, default `GDRIVE_TOKEN`) |
| `slack` | Slack channel transcripts (`SLACK_TOKEN`; ids rendered as-is) |
| `confluence` | Confluence pages (`CONFLUENCE_TOKEN` + `connector.api_endpoint` site URL) |
| `imap` | An email mailbox (`IMAP_CREDENTIALS` as `user:password`; `path` = mailbox) |
| `api` | Experimental — an HTTP API source |
| `s3` | Experimental — an S3-style object store |

Third-party connector plugins add further types by name — see the
[Connector SDK](../reference/connector-sdk.md).

## Notion

Create an internal integration at `notion.so/my-integrations`, share the
pages with it, and export the token in the environment — it never lands in
config:

```yaml
sources:
  - name: team-notion
    type: notion
    path: /unused            # required by the schema; Notion ignores it
    include: []
    connector:
      api_key_env: NOTION_TOKEN   # default; name of the env var
```

Pages are listed through Notion's search API and rendered to deterministic
Markdown (headings, lists, to-dos, quotes, code, nested blocks). Sync is
incremental: unchanged pages (by `last_edited_time`) are skipped before
any block is fetched, so a large workspace re-syncs in seconds. Page
`created_by` / `last_edited_by` ids are captured for the upcoming
permission-aware retrieval work.

## A minimal source

```yaml
sources:
  - name: my-repo            # stable id; appears in stable node IDs
    type: repository
    path: /workspace         # must resolve under an allowlisted root
    enabled: true
    include:
      - "**/*.py"
      - "**/*.md"
    exclude:
      - "**/.git/**"
      - "**/__pycache__/**"
    chunking:
      enabled: true
      strategy: semantic     # or heading_or_page for documents
      max_chars: 4000
      overlap_chars: 400
    sync:
      on_startup: true
      on_file_change: debounce
      interval_seconds: 900
```

## Include / exclude

- `include` is a list of glob patterns. A file must match at least one to be
  indexed.
- `exclude` removes matches. Secrets (`.env*`, private keys, `.pem`/`.key`) are
  excluded by default via `security.default_exclude_secrets`; keep those
  patterns in `exclude` too for defense in depth.
- **Extension globs control more than filtering.** Admitting an image extension
  (`**/*.png`) builds the image captioner; admitting an audio extension
  (`**/*.wav`) builds the transcriber. See
  [Multi-modal ingest](multimodal-ingest.md).

The reference `syncsage.example.yaml` ships a thorough `exclude` list (`.git`,
`node_modules`, `dist`, `build`, virtualenvs, state/vault/exports) — copy it as
a baseline.

## Sync modes

Run a sync with `syncsage sync`:

```bash
syncsage sync --config syncsage.yaml --source my-repo --mode incremental
syncsage sync --config syncsage.yaml --all --mode full
```

| Mode | Behavior |
|---|---|
| `incremental` | Uses connector checkpoints + content hashes to skip unchanged artifacts. The default. |
| `full` | Rebuilds artifact, chunk, graph, manifest, and checkpoint state for a source. |
| `validate_only` | Checks connector health and readability without writing index artifacts or manifests. |
| `repair` | Rebuilds missing or invalid state from manifests and database rows. (Also available as `syncsage repair`.) |

Indexing is **idempotent**: re-syncing unchanged content produces the same state
(content `sha256` + stable IDs), so a no-op sync skips everything.

## When syncs run automatically

The `sync:` block on each source, plus the global `sync:` block, control
automatic syncing:

- **On startup** — `sync.on_startup: true` (and `sync.startup.full_validation`).
- **On file change** — the watcher (`sync.watcher`) debounces filesystem events.
  Watcher reliability varies across Docker mount types; keep the scheduler on as
  a fallback.
- **On git commit / branch switch** — `sync.git` re-indexes or validates.
- **On a schedule** — `sync.scheduler.interval_seconds` (default 900s).

## Validate before you run

```bash
syncsage validate syncsage.yaml      # config shape + allowlist + paths
syncsage doctor --config syncsage.yaml   # runtime environment checks
syncsage config show --effective --config syncsage.yaml   # resolved config
```

See the full key-by-key reference in [Configuration](../configuration.md).
