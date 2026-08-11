# Configuration

pheasant reads YAML from `/config/pheasant.yaml` by default. Start from `pheasant.example.yaml` and mount it read-only into the container.

The local `pheasant.yaml` copy is intentionally ignored by git because it contains host-specific mount assumptions. Commit changes to `pheasant.example.yaml` when you want to update the shared pattern. Docker Compose env files generated under `.pheasant/` are ignored.

For a one-line local run, use a profile:

```bash
pheasant start --profile quickstart --config pheasant.yaml
```

Config is resolved as base defaults + profile + YAML + `--set` overrides. Inspect the result with:

```bash
pheasant config show --effective --profile dev --config pheasant.yaml
```

## How to use this guide

1. Copy `pheasant.example.yaml` to your runtime config path.
2. Keep the top-level sections in place and only edit values you need.
3. For each setting below, choose values based on your deployment mode and data sensitivity.
4. Validate by starting pheasant and checking startup logs for loaded source counts and enabled transports.

---

## Top-level sections

| Section | Purpose | Required |
|---|---|---|
| `deployment` | Image and mount hints used by deployment tooling/templates. | Recommended |
| `pheasant` | Instance identity, environment label, and core filesystem roots. | Yes |
| `server` | API/MCP/UI network bindings and feature toggles. | Yes |
| `storage` | Database/graph/manifests locations and state limits. | Yes |
| `search` | Retrieval modes and ranking behavior. | Yes |
| `ingestion` | Turning binary/markup files (documents, images, audio) into indexable text. | Optional |
| `sync` | Watcher, git polling, schedule, idempotency, and concurrency behavior. | Yes |
| `graph` | Knowledge-graph density (concept-node threshold, WASM acceleration). | Optional |
| `obsidian` | Export controls for notes/canvas/frontmatter/backlinks/tags. | Optional |
| `security` | Path allowlisting, source-read protections, and ACL enforcement. | Strongly recommended |
| `synapse` | Federation into a Synapse fleet (contract publishing, signing). | Optional, standalone-safe |
| `memory` | Agent-memory consolidation policy (TTL decay, supersede archiving). | Optional |
| `assistant` | Grounded chat over the index (the UI's chat layer). Query-time only. | Optional |
| `sources` | All indexed repositories/folders/files/URLs (incl. per-source `taxonomy`). | Yes |

> **Note:** this table's row order follows `PheasantConfig`'s field order in
> `src/pheasant/config/schema.py`, not necessarily the order sections appear
> below. `tests/test_config_surface_freshness.py` fails CI if a new top-level
> settings block lands in that file without a mention here and without being
> reachable from `pheasant setup` — see [Set pheasant up](how-to/setup.md).
> You rarely need to write any of this by hand: `pheasant setup` asks about
> every section, and the web UI's Settings page edits most of them live.

---

## `deployment`

### `deployment.compose`

| Key | Type | Example | What it controls |
|---|---|---|---|
| `image_repository` | string | `ghcr.io/esatt10/pheasant` | Container image registry/repository for Compose-based runs. |
| `image_tag` | string | `0.1.3` | Image version tag used by deployment helpers. |
| `workspace_path` | path-like string | `./workspace` | Host path mounted to pheasant `workspace_root`. |
| `vault_path` | path-like string | `./vault` | Host path mounted to pheasant `vault_path`. |

---

## `pheasant` (core instance settings)

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | `local-pheasant` | Used as instance/knowledge-base identifier. |
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
| `host` | string | `0.0.0.0` | Bind address. `pheasant up` generates `127.0.0.1`; containers keep `0.0.0.0` (loopback inside a container is unreachable from the host) and compose publishes them to `127.0.0.1` instead. |
| `port` | integer | `8765` | Primary service port. |

The API is unauthenticated, so the bind address is a security control, not
just a networking detail — see [security.md](security.md#trust-model-for-the-http-api).

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
| `api.cors_origins` | list[str] | localhost dev/UI origins | Browser origins allowed to call the API. The shipped UI proxies `/api/*` same-origin and needs no entry here. |
| `api.cors_allow_all_origins` | bool | `false` | Restores `Access-Control-Allow-Origin: *`. The API is unauthenticated — only enable behind an authenticating ingress. |
| `ui.enabled` | bool | `true` | Enables web UI routes (if packaged). |
| `ui.graph_visualization` | bool | `true` | Enables graph visualization features in UI. |

---

## `storage` (state + persistence)

| Key | Type | Default | Notes |
|---|---|---|---|
| `graph_format` | string | `node_link_json` | Graph serialization format. |
| `graph_snapshot_interval_seconds` | integer | `900` | Graph snapshot cadence. |
| `sqlite_path` | absolute path | `/state/pheasant.db` | Main SQLite database file. |
| `graph_path` | absolute path | `/state/graphs` | Directory for graph snapshots. |
| `manifest_path` | absolute path | `/state/manifests` | Directory for source manifests. Connector checkpoints are stored in SQLite. |
| `max_state_size_gb` | integer | `10` | Soft state budget for cleanup/policy logic. |
| `compression.enabled` | bool | `true` (example) | Optional compression toggle for persisted artifacts. |
| `compression.algorithm` | string | `zstd` (example) | Compression codec name. |
| `retention.keep_snapshots` | integer | `10` (example) | Snapshot retention count target. |
| `retention.keep_event_days` | integer | `30` (example) | Event retention age target in days. |

> Notes:
> - If `sqlite_path`, `graph_path`, or `manifest_path` are omitted, they are derived from `pheasant.state_path`.

---

## `search` (retrieval behavior)

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_mode` | string | `hybrid` | Typical modes: keyword, path, graph, hybrid (implementation-dependent). |
| `keyword.enabled` | bool | `true` (example) | Enables keyword index/query path. |
| `keyword.engine` | string | `sqlite_fts5` (example) | Keyword backend engine. |
| `embeddings.enabled` | bool | `false` | Enables embed-on-sync + `mode=vector` self-search (Synapse 21.4). |
| `embeddings.provider` | string | `openai-spec` | `openai-spec` (OpenAI-compatible HTTP endpoint) or `stub` (deterministic, offline). |
| `embeddings.model` | string | `text-embedding-3-small` | Embedding model name (must match the Synapse fleet pin when federated). |
| `embeddings.base_url` | string | `https://api.openai.com/v1` | OpenAI-spec endpoint base; `POST {base_url}/embeddings`. |
| `embeddings.api_key_env` | string | `OPENAI_API_KEY` | Name of the env var holding the API key (key never lands in config/state). |
| `embeddings.dimensions` | integer \| null | `null` | Unset by default — the `dimensions` request field is simply omitted, so the provider returns the model's own native size (e.g. 1536 for `text-embedding-3-small`, 3072 for `text-embedding-3-large`). Set an explicit number only to shrink vectors for storage (OpenAI's `-3` models support this) or to pin an exact size across a Synapse fleet. |
| `embeddings.batch_size` | integer | `64` | Texts per embedding HTTP request. |
| `vector_store.provider` | string | `lancedb` | `lancedb` (optional `[vector]` extra) or `numpy` (always-available flat file). |
| `vector_store.path` | absolute path | `<state>/vectors` | Vector index root; vectors live under `<path>/<kb_id>/`. Created only when embeddings are enabled. |
| `ranking.prefer_exact_path_matches` | bool | `true` (example) | Boost exact path matches. |
| `ranking.prefer_recent_commits` | bool | `true` (example) | Boost content tied to recent commits. |
| `ranking.graph_neighbor_boost` | bool | `true` (example) | Boost graph-adjacent matches. |
| `ranking.max_results_default` | integer | `10` | Default result count cap. |
| `wasm_relationship_search` | bool | `false` | Run `graph_search._scan_edges` through the vendored WASM accelerator (Synapse 34.5b) instead of pure Python. Needs the `[wasm]` extra; falls back to pure Python on any failure or if the extra is missing — never a correctness dependency. A consistent, growing win (2-8x at 34.4's benchmark scale) on the relationship-search query path. |

---

## `ingestion` (multi-modal: image captioning + audio transcription)

Only takes effect for a source whose `include` globs admit an image or
audio extension — a text-only region builds neither captioner nor
transcriber and stays byte-identical to a pre-25.4 config. Captions/
transcripts flow through the normal chunk → embed → graph path like any
other text; an authored sidecar (`<image>.caption.txt` /
`<audio>.transcript.txt`) always wins over the model.

| Key | Type | Default | Notes |
|---|---|---|---|
| `captioner.provider` | string | `stub` | `stub` (deterministic, offline, default — caption = template over file name + digest of bytes) or `openai-spec` (vision-capable chat model, `POST {base_url}/chat/completions` with an `image_url` part). |
| `captioner.model` | string | `gpt-4o-mini` | Vision model name (only used by `openai-spec`). |
| `captioner.base_url` | string | `https://api.openai.com/v1` | OpenAI-spec endpoint base. |
| `captioner.api_key_env` | string | `OPENAI_API_KEY` | Env var name holding the key; the key itself never lands in config. |
| `captioner.prompt` | string | `Describe this image in one concise sentence for search indexing.` | Prompt sent with each image. |
| `transcriber.provider` | string | `stub` | `stub` (deterministic, offline, default — no audio library, no network) or `openai-spec` (`POST {base_url}/audio/transcriptions`, multipart upload). |
| `transcriber.model` | string | `whisper-1` | Speech-to-text model name (only used by `openai-spec`). |
| `transcriber.base_url` | string | `https://api.openai.com/v1` | OpenAI-spec endpoint base. |
| `transcriber.api_key_env` | string | `OPENAI_API_KEY` | Env var name holding the key. |

See [Multi-modal ingest](how-to/multimodal-ingest.md).

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

### Size guardrails (`sync.limits`)

A source may point at any readable path, which makes "I accidentally indexed
my home directory" a realistic mistake. These limits are checked **during**
traversal, before any file is read, so an oversized source is refused rather
than consuming memory until the process dies. Set any field to `null` to
disable that limit.

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_files` | integer\|null | `50000` | Matching files, after include/exclude. |
| `max_file_size_mb` | integer\|null | `25` | Skip any single file larger than this. Skipped files are reported, not fatal. |
| `max_total_mb` | integer\|null | `4096` | Total matched content. |
| `follow_symlinks` | bool | `false` | Home directories routinely contain links that escape the root or loop. |

A source can override the whole block with `sources[].limits`.

**A source over budget indexes nothing.** A partial index would be
non-deterministic, and silently indexing the first N files of a home
directory is worse than a clear stop. The sync returns
`status: "limit_exceeded"` with a message naming the limit and the largest
subtrees. Your options are: narrow it (`max_depth`, tighter `include`, more
`exclude`), raise the limits, or sync once with `--full-scan`.

### Knowing the size first (`pheasant scan`)

```bash
pheasant scan -c pheasant.yaml            # every enabled source
pheasant scan -s notes --depth 2 --json   # one source, machine-readable
```

`scan` walks without reading or indexing anything and reports the file
count, total size, largest subtrees, oversized files, and a **files-by-depth
table** so a depth cap can be chosen from evidence rather than guessed. It
also reports whether the configured limits would refuse the sync. Also
available as `POST /sources/{id}/scan` and the `scan_source` MCP tool.

### Per-run traversal toggles

`--depth N` and `--full-scan` apply to one invocation and are never written
back to the source config, so a one-off wide sync cannot silently become the
standing behavior of a scheduled one.

| Surface | Depth cap | Full scan |
|---|---|---|
| CLI | `pheasant sync --depth N` | `pheasant sync --full-scan` |
| HTTP | `{"depth": N}` on `/sync`, `/sync/{id}` | `{"full_scan": true}` |
| MCP | `max_depth=N` on `sync_source`/`sync_all` | `full_scan=true` |

`--full-scan` lifts both the depth cap and the size budget — the explicit
"yes, index all of it" switch.

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

## `ingestion` (binary/markup files → indexable text)

Some files carry text that cannot be reached by decoding the bytes: a PDF
stores it in compressed content streams, a DOCX in zipped XML, an image or an
audio file not at all. This section configures the handlers that turn those
into text, which then flows through the **normal** chunk → embed → graph path
like any other document.

Every handler is **opt-in by source include**. A source whose `include` globs
admit only code/markdown/config builds none of them, and behaves exactly as it
would if this section did not exist. `pheasant setup` and `pheasant up` emit
`**/*` for detected mixed document folders; hand-written sources can use
explicit document extensions instead.

| Handler | Extensions | Built when `include` admits | Network? |
|---|---|---|---|
| `extractor` | `.pdf` `.docx` `.pptx` `.xlsx` `.doc` `.rtf` `.epub` (+ `.html` `.htm` `.xhtml` when `html_text`) | any of those | **never** |
| `captioner` | `.png` `.jpg` `.jpeg` `.webp` `.gif` | an image extension | only if `provider: openai-spec` |
| `transcriber` | `.wav` `.mp3` `.m4a` `.flac` `.ogg` | an audio extension | only if `provider: openai-spec` |

### `ingestion.extractor` (document text)

Without an extractor, a document is **accepted and then silently produces no
text**: the artifact is discovered, hashed, typed `document` and given a graph
node, but contributes **zero chunks** — findable by its path, invisible by its
content. Configuring the extractor is what makes the contents searchable.

Seven formats are handled:

| Format | Extension | How the text is reached | Notes |
|---|---|---|---|
| PDF | `.pdf` | content streams (`zlib` + operator scan) or pymupdf | Only format with a sandboxed option |
| Word (OOXML) | `.docx` | `word/document.xml` | Includes tables |
| Word (legacy) | `.doc` | OLE2 compound file → FIB → piece table | Word 97-2003; pre-97 layouts are refused, not guessed |
| PowerPoint | `.pptx` | `<a:t>` runs per slide, in slide order | **Speaker notes are indexed too** |
| Excel | `.xlsx` | sheets + `sharedStrings`, tab-separated rows | Sheet names included |
| RTF | `.rtf` | control-word tokenizer | Requires the `{\rtf` signature |
| EPUB | `.epub` | OPF **spine** order → XHTML → text | Spine, not filename order |

Unlike the captioner/transcriber, no provider here uses a model or makes a
network call — the text is already in the file — so every option is fully
offline and deterministic.

| Key | Default | Purpose |
|---|---|---|
| `provider` | `auto` | `auto` \| `native` \| `builtin` \| `sandboxed` (see below) |
| `html_text` | `false` | Strip markup from HTML/XHTML so prose indexes instead of tags |

**Providers**

| Provider | PDF path | DOCX path | Notes |
|---|---|---|---|
| `auto` | `pymupdf`, else builtin | `python-docx`, else builtin | Default. Keeps whichever yields text; never raises into a sync. |
| `native` | `pymupdf` | `python-docx` | Best fidelity: CID/Type0 fonts, custom encodings, complex layout. Both libraries are already core dependencies. |
| `builtin` | `zlib` + content-stream scan | `zipfile` + `xml.etree` | **Standard library only.** No third-party imports at all. |
| `sandboxed` | builtin tokenizer inside the WASM sandbox | same as `builtin` | Fuel + memory cap, zero host capabilities. Needs `pip install 'pheasant-kb[wasm]'`. |

`native` also reads **EPUB** through pymupdf, which lays the book out and walks
it in reading order — a genuine upgrade over the builtin spine walk.

For **PPTX, XLSX, RTF and legacy DOC**, `native` and `builtin` are the *same
code path*. No third-party reader for those formats exists in this project's
dependency tree (`python-docx` handles only OOXML Word; `pymupdf` does not open
them), and the builtin readers are complete for them — in the OOXML and EPUB
formats the XML *is* the text, and RTF is a text format by definition. Listing
`native` as an upgrade there would be a pretend distinction.

**Authored sidecars.** If `<file>.extract.txt` sits next to a document, its
contents are used **verbatim** and no extractor runs — the offline way to give
an image-only scanned PDF real searchable text (mirrors the `.caption.txt` /
`.transcript.txt` sidecars).

**Why `html_text` defaults to off.** `.html` and `.xml` have always been
indexed as *raw markup* (tags, `<script>`, CSS included). Stripping them is an
improvement, but it changes the indexed text and therefore chunk boundaries of
an existing knowledge base, so it is an explicit opt-in rather than a surprise
on upgrade.

**When to choose `sandboxed`.** PDF is a classic hostile-input parser target,
and PDFs arriving from connectors (Google Drive, Slack, Confluence, IMAP) are
not authored by you. In-process, that parse runs with the sync worker's ambient
authority — every configured connector's API token in the environment, a
writable `/state`, network egress. `sandboxed` runs the tokenizer under a fuel
cap, a linear-memory cap, and **no host capabilities at all**. It is *not* a
fallback-on-failure path: if `wasmtime` is missing it raises with an actionable
hint rather than quietly extracting unsandboxed, because an operator who asked
for isolation must not silently get none.

Fidelity trade-off: `sandboxed` and `builtin` handle uncompressed and
FlateDecode streams with single-byte font encodings — the large majority of
real text PDFs — but do not decrypt encrypted PDFs, decode LZW/CCITT streams,
or resolve Type0/CID font CMaps. `native`/`auto` handle those. Pick
`sandboxed` when the *input* is untrusted; pick `auto` when it is yours.

```yaml
ingestion:
  extractor:
    provider: auto
    html_text: false
```

### `ingestion.captioner` / `ingestion.transcriber`

See [Multi-modal ingest](how-to/multimodal-ingest.md) for the full walkthrough.

| Key | Default (captioner) | Default (transcriber) |
|---|---|---|
| `provider` | `stub` | `stub` |
| `model` | `gpt-4o-mini` | `whisper-1` |
| `base_url` | `https://api.openai.com/v1` | `https://api.openai.com/v1` |
| `api_key_env` | `OPENAI_API_KEY` | `OPENAI_API_KEY` |
| `prompt` | (caption instruction) | — |

`api_key_env` is the **name** of an environment variable; the key itself never
lands in config or on disk.

---

## `graph` (knowledge-graph density)

| Key | Type | Default | Notes |
|---|---|---|---|
| `concept_min_documents` | integer | `2` | Distinct documents that must share a term before it becomes a `concept` node. A concept exists to link the documents that share it; one mentioned by a single document is pure weight — measured on a real corpus, 74.2% of concept nodes were single-document and concepts made up 87.2% of a bloated graph. Set to `1` to keep every term as a node (pre-2026-08 behavior). Nothing becomes unfindable at higher values: the term stays on `concept_terms`/`artifact_terms` and in searchable text either way. |
| `wasm_cross_source_resolution` | bool | `false` | Run `resolve_cross_source_edges` (import/link resolution across sources) through the vendored WASM accelerator (Synapse 34.5a) instead of pure Python. Needs the `[wasm]` extra; falls back to pure Python on any failure or if the extra is missing. Conditional win per the 34.4 benchmark — loses to Python below roughly 1,300-2,500 edges, wins modestly above it; opt in for large/growing multi-source graphs, leave off for small ones. |

---

## `obsidian` (output options)

| Key | Type | Default / Example | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Master toggle for Obsidian exports. |
| `write_mode` | string | `upsert` | Export strategy (`upsert`, etc.). |
| `note_root` | string | `pheasant` | Top-level folder/note namespace in the vault. |
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
| `tags.base` | list[string] | `[pheasant]` | Base tags appended to generated notes. |
| `tags.by_source_type` | bool | `true` | Add tags based on source type. |

---

## `security`

| Key | Type | Example | Notes |
|---|---|---|---|
| `allow_workspace_roots` | list[path] | `[/workspace, /vault]` | Allowed root prefixes for registered source paths. |
| `read_only_sources` | bool | `true` | Prevent source mutation operations. |
| `deny_path_traversal` | bool | `true` | Block `..` traversal and unsafe resolution. |
| `allow_user_selected_source_paths` | bool | `true` | Let a source name any readable path, not just one under `allow_workspace_roots`. This is what makes "point it at anything" work; see the security notes on what compensates for it. |
| `default_exclude_secrets` | bool | `true` | **Always** union `SECRET_EXCLUDES` into every filesystem source's excludes. Unlike the rest of `DEFAULT_EXCLUDES`, supplying your own `exclude` list does not drop these. |
| `acl_enforced` | bool | `false` | Master toggle for principal-aware retrieval (Step 32.x). `false` = every pre-32 deployment stays byte-identical. When `true`, `search_context` filters candidates against each artifact's captured ACL before merge/return. |
| `default_visibility` | string | `public` | How an un-ACL'd artifact (no connector-captured ACL, e.g. a plain filesystem source) is treated once `acl_enforced` is on: `public` keeps it searchable by anyone, `private` requires an authenticated principal. |
| `groups` | map[str, list[str]] | `{}` | Config-mapped `principal -> [group, ...]` identities, unioned with any IdP-synced groups at query time. |
| `idp.enabled` | bool | `false` | Turn on SCIM 2.0 group-directory sync (Step 32.4). Disabled by default — `groups` above still works with zero env vars. |
| `idp.provider` | string | `scim` | Directory protocol. |
| `idp.base_url` | string | `""` | SCIM `/Groups` listing endpoint base. |
| `idp.api_key_env` | string | `IDP_TOKEN` | Env var holding the bearer token; never stored in config. |
| `idp.sync_interval_minutes` | integer | `60` | How often the scheduler beat (or `POST /security/idp/sync`) refreshes the mapping. |
| `idp.staleness_max_minutes` | integer | `1440` | SLA: a mapping older than this **fails closed** (grants nothing) until the next successful sync. |

---

## `synapse` (federation into a Synapse fleet, optional)

Standalone-safe by construction: every router-facing behavior no-ops with
the defaults below, so a region that never sets `router_url` behaves
exactly like a router-less pheasant. Read
[Attach to a Synapse fleet](how-to/attach-to-synapse.md) before enabling.

| Key | Type | Default | Notes |
|---|---|---|---|
| `publish` | bool | `false` | Gate contract publication + the NDJSON sync-event stream. |
| `router_url` | string \| null | `null` | Synapse router base URL, e.g. `http://synapse-router:8000`. When set, each successful sync POSTs `sync.completed` (with the inline contract) to `<router_url>/v1/synapse/events` — failures are logged, never raised. |
| `fleet_id` | string \| null | `null` | Fleet label stamped into the published contract. |
| `endpoint` | string \| null | `null` | This region's reachable base URL, e.g. `http://my-region:8765` (the router pulls `GET /contract` from here). |
| `webhook_timeout_seconds` | float | `5.0` | Timeout for the router-webhook POST. |
| `signing_key_ref` | string \| null | `null` | Secret *reference* — `env://NAME` or a bare env-var name — resolving to a base64 32-byte Ed25519 seed (Step 24.4). Unset (default): `integrity.signature` stays `null` and nothing changes. The plaintext key never lands in config or on disk. |

---

## `memory` (agent-memory consolidation, optional)

Governs the built-in `memory` source type (Step 33.x): agents write
records via MCP `memory_write` / `POST /memory`, which land as append-only
frontmatter Markdown files indexed by the ordinary pipeline — recall is
just search. This block only controls **consolidation** (archiving), not
whether the memory source itself is registered. See
[Agent memory](how-to/agent-memory.md).

| Key | Type | Default | Notes |
|---|---|---|---|
| `consolidation_enabled` | bool | `true` | Archive superseded records (an explicit correction) and per-scope TTL-expired records on the scheduler beat or via `memory_consolidate` / `POST /memory/consolidate`. Archiving renames `<id>.md` → `<id>.md.archived` in place — bytes preserved, never deleted — then a full re-sync prunes it from the index. |
| `session_ttl_days` | integer \| null | `null` | TTL for `session`-scoped records. `null` = never expires by age. |
| `user_ttl_days` | integer \| null | `null` | TTL for `user`-scoped records. |
| `org_ttl_days` | integer \| null | `null` | TTL for `org`-scoped records. |

---

## `assistant` (grounded chat)

Powers the UI's chat panel and `POST /assistant/chat`: retrieve from your own
index, cite the passages, surface graph facts, then ask a chat model to write
the answer from those passages alone.

**This is a query-time surface only.** No LLM ever runs during indexing, so
enabling it does not affect determinism — re-syncing unchanged content still
produces byte-identical state. With no provider reachable the assistant still
answers *extractively* (top passages + citations + facts), which is the
default and works fully offline.

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | `false` makes `/assistant/chat` return 403. |
| `provider` | str | `auto` | `auto` \| `anthropic` \| `openai` \| `gemini` \| `none`. `auto` picks the first provider whose key env var is set, in the order Anthropic → OpenAI → Gemini. |
| `model` | str \| null | `null` | Provider default when unset (`claude-sonnet-5`, `gpt-5.6-luna`, `gemini-2.5-flash`). |
| `base_url` | str \| null | `null` | Point at a gateway or self-hosted OpenAI-spec endpoint. |
| `api_key_env` | str \| null | `null` | Read the key from a differently-named variable. Defaults to the provider's own (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`). |
| `allow_session_keys` | bool | `true` | Let a UI user paste a key for their browser session. Held in server memory behind an opaque token — never written to config, `/state`, or logs; dropped on expiry, revoke, or restart. Set `false` to require the env var. |
| `session_key_ttl_minutes` | int | `720` | Lifetime of a session-supplied key. |
| `max_context_chunks` | int | `8` | Passages retrieved and offered to the model. |
| `max_output_tokens` | int | `4096` | Per-answer output cap sent to the provider. |
| `request_timeout_seconds` | float | `90.0` | Provider HTTP timeout. A timeout degrades to the extractive answer rather than erroring. |
| `max_facts` | int | `12` | Graph facts surfaced per answer, collected round-robin across the cited sources. |
| `workflow` | str | `auto` | Which agent workflow answers a question: `auto` \| `knowledge-summary` \| `agentic` \| `simple` \| any registered plugin name. `auto` = `agentic` when the `[agent]` extra is installed *and* a model is reachable, else `simple`. An unknown or failing workflow degrades to `simple` with the reason attached to the answer. |
| `workflow_options` | dict | `{}` | Per-workflow tuning, keyed by workflow name, merged over that workflow's defaults. Callers may override any key per request. |
| `retrieval` | block | see below | Typed retrieval criteria — the same knobs, with names, validation and a UI. |

### `assistant.retrieval` — how hard to look before answering

These knobs already existed as untyped keys inside `workflow_options`,
documented only in a workflow module's `DEFAULTS` dict. This block is their
typed home, which is what makes them validated, editable from the UI
(Settings → Retrieval tuning), and readable by an agent over MCP
(`describe_retrieval`).

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_rounds` | int \| null | `2` | plan → retrieve → grade turns before answering with what is in hand. `1` disables the re-plan loop. |
| `per_query_results` | int \| null | `6` | Passages fetched per query per search mode. |
| `max_context_passages` | int \| null | `10` | Total passages offered to the answering step. |
| `retrieval_modes` | list \| null | `["text", "vector"]` | Modes to fan out over. `vector` is dropped automatically when no vector index is built, so leaving it on is safe. |
| `expand_graph` | bool \| null | `true` | Walk the graph out of the best hits, reaching documents that share no vocabulary with the question. |
| `expand_depth` | int \| null | `1` | Hops to walk when expanding. |
| `expand_per_node` | int \| null | `3` | Neighbours taken per expanded node. |
| `grade_evidence` | bool \| null | `true` | Ask the model to grade its own evidence before answering. |
| `verify_citations` | bool \| null | `true` | Drop `[n]` markers that do not resolve to a real citation. |
| `max_facts` | int \| null | `12` | Graph facts surfaced alongside the answer. |

**Precedence is deliberately low.** Values merge in this order, later winning:

```
workflow DEFAULTS  <  assistant.retrieval  <  assistant.workflow_options  <  per-request options
```

So a config that already tuned `workflow_options` is completely unaffected by
this block's arrival, and an agent overriding a criterion for one call still
wins over both. A field left `null` is **not merged at all** — the workflow's
own default applies — which is what keeps this additive rather than a second
source of truth for values it does not care about.

Editable live: `GET`/`PUT /assistant/retrieval`. Retrieval is query-time only,
so a change applies to the next question with no restart and no re-index.

```yaml
assistant:
  retrieval:
    max_rounds: 3
    max_context_passages: 16
    retrieval_modes: ["text", "vector", "graph"]
```

**The key never lands in config.** Both routes are indirections: an
environment variable *name* here, or a runtime token in the browser. Nothing
in this file is a secret.

The `agentic` workflow is a LangGraph state graph (classify → plan → retrieve →
expand → grade → synthesize → verify) and needs the optional extra:

```bash
pip install 'pheasant-kb[agent]'
```

```yaml
assistant:
  workflow: agentic
  workflow_options:
    intent: auto            # auto | knowledge | procedural
    max_rounds: 3
    retrieval_modes: [hybrid, vector, graph]
    expand_depth: 2
    passage_chars: 6000     # how much of each cited file the model sees
```

`classify` reads the question as a **knowledge summary** ("what does this
repository do") or a **procedural** one ("how do I use this tool") and shifts
retrieval, the sufficiency bar and the answering prompt to match — breadth
over depth for the first, depth and real code examples for the second.
`knowledge-summary` is the same graph with that reading pinned. See
[Customize the question-answering workflow](how-to/agent-workflows.md).

Both workflows send the model whole **files**, rebuilt from their chunks with
line spans and metadata, rather than the 500-character search preview. Code and
config are never excerpted; large prose is cut to the matched neighbourhood.

Third-party workflows register under the `pheasant.agent_workflows`
entry-point group, the same plugin shape as the
[Connector SDK](reference/connector-sdk.md).

See the how-to guides: [Ask your knowledge base](how-to/chat-and-ui.md) and
[Customize the answering workflow](how-to/agent-workflows.md).

---

## `sources` (per-source configuration)

Each source item supports:

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | none | Unique source id/name. |
| `type` | enum \| plugin name | `single_file` | One of `repository`, `markdown_folder`, `obsidian_vault`, `document_folder`, `web_collection`, `single_file`, `s3`, `api` — or any installed connector plugin (`notion`, `gdrive`, `slack`, `confluence`, `imap`, or your own). `GET /sources/types` lists what this deployment accepts. |
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

### `sources[].taxonomy`

Structural taxonomy extraction for **books, procedures and legal documents** —
the outline the document already declares (Part / Chapter / Article / Section /
`§ 12.3` / `1.2.3` / `(a)`), turned into retrieval structure.

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Set it when registering the source. |
| `max_depth` | int | `6` | Deepest heading level to keep (clamped 1-6). |
| `detect` | list | `[]` (all) | Narrow the rule set: `markdown`, `keyword`, `code`, `numbered`, `lettered`, `caps`. |
| `graph_nodes` | bool | `true` | Emit `heading` nodes + `has_heading` edges. |
| `split_on_sections` | bool | `true` | Cut chunks at section boundaries so one chunk is one section. |

With it on, three things happen on every sync:

1. **Chunks are cut and labelled per section.** Each chunk carries its
   breadcrumb in `chunks.heading_path`, which `chunks_fts` indexes at BM25
   weight 2.0 — double the body text. A search hit then reports *which
   section* matched (`heading_path` on the result), not just which file.
2. **`heading` graph nodes and `has_heading` edges are emitted**, with a
   section `contains` its subsections — so the taxonomy is a traversable tree
   using the same `contains` edge the directory hierarchy uses.
3. **`GET /taxonomy`** renders the outline per document, with any numbering
   defects it found (gaps, duplicates, backwards numbering).

Retrieval can then be **restricted to one section**: `section` on
`POST /search` and MCP `search_context(section=...)` matches the breadcrumb, so
`§ 12.3`, `Article IV` or a section's wording all reach it, and naming a parent
returns everything nested under it. Graph hits are excluded under a section
filter — a symbol is not inside a document section.

```yaml
sources:
  - name: contracts
    type: document_folder
    path: /workspace/contracts
    include: ["**/*.pdf", "**/*.docx"]
    taxonomy:
      enabled: true
```

**Why it is off by default, and per source rather than global.** The numbering
rules are genuinely ambiguous on prose: `1. Introduction` in a standards
document is a section, `1. Buy milk` in a note is a list item, and nothing in
the line distinguishes them. Length and punctuation filters reduce the
confusion but cannot remove it. Enabling it per source is how you say "this
corpus really is structured documentation". It also changes what the FTS index
holds for that source, so it wants a deliberate `--mode full` re-sync.

**Ordinal reconciliation.** A heading's own number decides its parent wherever
it can, so mixed numbering works: `4.2` attaches to whichever heading *is* `4`
— including a roman `ARTICLE IV`, since `IV` parses to `(4,)` — while `§ 12.3`
refuses an ancestor whose ordinal is not a prefix of its own and climbs past the
Article to the unnumbered title above. `§ 12A` is treated as a *sibling* of
`§ 12`, because inserting a section is not nesting one. Lettered items (`(a)`,
`(iv)`) are positions among siblings and are placed by nesting only.

Each `heading` node stores its parsed ordinal (`ordinal_parts`,
`ordinal_series`, `ordinal_suffix`), so a section is queryable by citation.

**Sequence reconciliation.** `GET /taxonomy` also reports numbering defects per
document in an `issues` list — `gap` (with the `missing` numbers), `duplicate`
and `out_of_order`. For a contract or a procedure, "is anything missing?" is the
question people actually ask, and once ordinals are parsed it is nearly free to
answer. Only gaps *between observed siblings* are reported: a series starting at
3 is an excerpt, not a defect.

**Residual ambiguity.** Seven letters are also roman numerals. A lone
`(c)`/`(d)`/`(l)`/`(m)` is read as the letter and a lone `(i)`/`(v)`/`(x)` as
the numeral, which gets both conventions right in sequence but misreads a letter
list that runs as far as `(i)`. Bounded on purpose: lettered ordinals never
decide hierarchy, so the worst case is one spurious `issues` entry.

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
- `deployment.compose.image_tag` in `pheasant.example.yaml` is one of the generated references that should be incremented to the new server version during release prep.
- Use `python scripts/sync_version.py --check` in CI/local validation to confirm all generated version references are synchronized.

If this check fails on merge/release automation, bump the project version and re-run the sync script so config and deployment manifests match.

---

## Deployment modality examples

### 1) Local developer workstation (Docker Compose)

Use this for single-machine local development with mounted host directories.

```yaml
deployment:
  compose:
    image_repository: ghcr.io/esatt10/pheasant
    image_tag: 0.1.3
    workspace_path: ./workspace
    vault_path: ./vault

pheasant:
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
pheasant:
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
  note_root: pheasant
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
      - pheasant
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
- Use `pheasant init --profile <name>` to generate a focused starter config.
- Use `pheasant doctor --profile <name> --config pheasant.yaml` before long-running syncs.
- Confirm every `sources[].path` is under an allowed security root.
- Trim `include` patterns first; then harden `exclude` patterns.
- Keep `scheduler.interval_seconds` enabled as a safety net even when watcher is on.
- Enable vector settings only if you intend to run an embedding/vector stack.
- For production, disable OpenAPI/UI if not required.
