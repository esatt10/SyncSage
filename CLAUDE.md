# CLAUDE.md — SyncSage

Primary context hand-off for any Claude agent working on this repository.
Read it first; it is intentionally dense. Anything not here should be
derivable from the code, `docs/`, or a single grep.

---

## 1. What this project is

**SyncSage** is a Docker-first, local-first **MCP context server** that
turns configured sources (git repositories, folders, single files,
Obsidian vaults, web collections, experimental API/S3) into a queryable
**knowledge graph** with hybrid self-search, for agents and humans.

Design pillars (do not violate):

- **Idempotent indexing** — re-syncing unchanged content produces the
  same state (content sha256 + stable IDs).
- **Incremental-by-default** — connector checkpoints + manifests skip
  unchanged artifacts.
- **Deterministic parsing** — no LLM calls in the indexing path; all
  enrichment is rule-based and reproducible. (The only sanctioned network
  call at sync time is the optional embedding provider, Phase 21.4, which
  must keep a stub/offline path.)
- **Persistence split** — `/state` (operational truth: SQLite + graph
  JSON + manifests), `/vault` (human-readable Obsidian projection),
  `/exports` (regenerable payloads). State dirs are **user data**:
  migrations must be one-shot, idempotent, and preserve originals.

### 1.1 SyncSage's second role: a Synapse brain region

Since 2026-06-10, SyncSage is also the **region** component of
**Synapse** — a federated knowledge-base system whose router (the
"nervous system") lives in the sibling **subjective-retrieval** repo.
Each SyncSage container publishes a **semantic contract** derived from
its own content; the router scores contracts to decide which regions to
query and fans out to each region's self-search. Read
`docs/SYNAPSE_INTEGRATION.md` before doing any Synapse-related work here.
Two iron rules: (1) the contract schema is canonical in
subjective-retrieval — this repo only vendors the exported JSON Schema +
fixtures under `contracts/`; (2) **no Python dependency between the
repos** — the boundary is contract JSON + HTTP, and a router-less
SyncSage must keep working unchanged.

---

## 2. Repository layout

```
SyncSage/
├── CLAUDE.md                  ← you are here
├── README.md
├── pyproject.toml             ← extras: [mcp], [dev]; (Phase 21.4 adds [vector]);
│                                core deps include numpy + zstandard (21.6A snapshots/backup)
├── syncsage.example.yaml      ← reference config (all sections)
├── Dockerfile                 ← python:3.12-slim, port 8765, /config/syncsage.yaml
├── docker-compose.yml         ← syncsage + optional syncsage-ui sidecar
├── deploy/                    ← kubernetes/ manifests, helm/ skeleton
├── docs/
│   ├── architecture.md        ← component overview
│   ├── graph_model.md         ← node/edge taxonomy + stable-ID grammar
│   ├── configuration.md       ← config reference
│   ├── mcp_tools.md / mcp_client.md
│   ├── deployment.md / security.md / troubleshooting.md
│   └── SYNAPSE_INTEGRATION.md ← region-side Synapse spec + Phase 21 plan
├── agent/                     ← legacy role prompts from the initial build
│                                (superseded for Synapse work by this file)
├── src/syncsage/
│   ├── cli.py                 ← Typer CLI: init/start/serve/validate/doctor/
│   │                            sync/repair/mcp/client-config/compose-env/config
│   ├── config/                ← schema.py (dataclasses), loader, profiles
│   │                            (quickstart/dev/team/cloud-hybrid), defaults
│   ├── sync/                  ← engine.py (SyncEngine), connectors.py
│   │                            (Filesystem/Web/API/S3), watcher.py,
│   │                            scheduler.py, git_monitor.py, debounce.py, locks.py
│   ├── ingestion/             ← pipeline.py (parse/hash/git-meta), chunking.py,
│   │                            content_types.py (py/md/txt/pdf/docx/html/xml/configs)
│   ├── graph/                 ← model.py, simple.py (SimpleMultiDiGraph +
│   │                            node-link JSON), builder.py (GraphBuilder),
│   │                            enrichment.py (Code/Markdown/Similarity passes)
│   ├── search/                ← sqlite_store.py (FTS5+BM25+term expansion),
│   │                            graph_search.py, hybrid.py (text|graph|hybrid)
│   ├── persistence/           ← state_store.py (SQLite SCHEMA), graph_store.py,
│   │                            manifest.py, paths.py
│   ├── registry/              ← source + knowledge-base registries
│   ├── mcp_server/            ← server.py (FastMCP), tools.py (SyncSageTools),
│   │                            resources.py, prompts.py, contracts.py
│   ├── api/app.py             ← FastAPI: /health /ready /sources /sync /search
│   │                            /graph /relevant-files /files /nodes /repos
│   │                            /config /obsidian /fs /sync/status|history
│   ├── obsidian/              ← exporter, templates, frontmatter, backlinks, canvas
│   ├── security/path_policy.py
│   ├── telemetry/  deployment/
├── ui/                        ← React+Vite: Cytoscape graph workspace, source
│                                mgmt, config editor (form+YAML+diff), search
└── tests/                     ← pytest; fixtures under tests/fixtures/ (sample
                                 workspace: notes, docs, mock repo)
```

Key entities: **knowledge base** (`kb_id` = `syncsage.name`) → **sources**
→ **artifacts** (stable ID `file:{source}:{relpath}:branch={b}`) →
**chunks** (+ FTS5) and graph nodes (**symbol/entity/concept/
external_reference**) with edges (contains/indexes/has_chunk/mentions/
references/imports/calls/similar_to). Stable-ID grammar and full taxonomy:
`docs/graph_model.md`.

---

## 3. Canonical commands

```bash
pip install -e ".[dev,mcp]"
pytest -q                                  # full suite, offline by design
ruff check src tests && ruff format --check src tests

syncsage up [PATH] [--no-serve]            # zero-config quickstart: detect
                                           #   (.obsidian/.git/folder) → generate
                                           #   config → index → serve (Step 30.1)
syncsage init --profile quickstart         # generate starter syncsage.yaml
syncsage validate && syncsage doctor       # config + environment checks
syncsage start                             # HTTP API + MCP on :8765
syncsage sync --source <name> --mode incremental|full|validate_only|repair
syncsage mcp --transport stdio             # standalone MCP server
syncsage client-config claude-code|cursor|vscode   # emit MCP client config
                                           #   (agents: --mode local|docker-exec|
                                           #    docker-run; Step 30.5)
syncsage config show                       # resolved config after profile+YAML+--set
docker compose up                          # container + optional UI sidecar
```

---

## 4. Rules for Claude

1. **Never put an LLM call in the indexing path.** Determinism is a
   product guarantee. Optional embeddings (21.4) must have a stub path so
   `pytest` stays network-free.
2. **Treat `/state` as user data.** Schema/layout changes ship a one-shot
   idempotent migration that preserves originals (`*.migrated` rename,
   never delete).
3. **Stable IDs are contracts.** Changing the ID grammar in
   `docs/graph_model.md` breaks every persisted graph — needs migration +
   explicit decision note in `docs/SYNAPSE_INTEGRATION.md` or a dated
   entry in the other repo's `docs/DECISIONS.md` if Synapse-relevant.
4. **Idempotency tests are the spine.** `tests/test_sync_idempotency.py`
   must stay green; any sync change adds cases there.
5. **Keep house style:** Typer CLI, dataclass config schema, ruff
   format+lint, pytest, Python ≥ 3.11, type hints.
6. **Never import subjective-retrieval.** The Synapse boundary is
   contract JSON + HTTP (see §1.1). Never hand-edit vendored files under
   `contracts/`.
7. **Standalone mode is sacred.** Every change must leave a router-less
   SyncSage fully functional; Synapse features no-op when
   `synapse.router_url` is unset.
8. **MCP tool surface is public API.** Renaming/removing tools in
   `mcp_server/tools.py` breaks deployed agents — additive evolution only,
   deprecate before remove.
9. **Cross-repo work** (anything marked [x-repo] in
   `docs/SYNAPSE_INTEGRATION.md`) follows the `syncsage-coordinator`
   skill in the subjective-retrieval repo: identical branch names in both
   repos, contract fixture parity (sha256), both test suites green before
   either push.
10. **Scope each session to one Phase-21 step** (or one bugfix). Write
    `runs/<ts>-synapse-<step>/SUMMARY.md` per the framework conventions.

---

## 5. Current Synapse work queue (Phase 21 — region hardening)

**Phase 21 region hardening is complete (2026-06-18).** Remaining Synapse work
lives in the sibling subjective-retrieval repo (Phase 22 finish — Step 22.5 —
plus Phases 23–26).

**Step 24.4 (A2A + Ed25519-signed contracts) landed here 2026-06-20 [x-repo].**
The publisher now **optionally signs** the contract's `integrity.signature`
(Ed25519) when `synapse.signing_key_ref` is set (a secret *reference* —
`env://NAME` or a bare env var name — resolving to a base64 32-byte Ed25519
seed; the plaintext key never lands in config or on disk). Unset → unsigned,
`integrity.signature: null`, and a **standalone SyncSage is unchanged**. New
`src/syncsage/synapse/signing.py` (`sign_body`/`signing_bytes`/`content_hash`/
`resolve_signing_key_ref`/`public_key_b64`); the `cryptography` import is gated
behind the new optional **`[a2a]` extra** (a region without `signing_key_ref`
needs no crypto dep; offline tests `importorskip("cryptography")`). The
signature covers byte-identical canonical body bytes to `_content_hash`, so the
router's `SemanticContract.verify_signature` accepts it — **out-of-band public
key** decision: the router carries the kb_id→pubkey trust store in *its* config,
so the contract wire format / vendored schema are **unchanged** (no re-vendor;
schema export asserted byte-identical in SR). A new vendored
`contracts/fixtures/signed-demo-region.v1.contract.json` (+ PARITY.json sha256
both repos) is the cross-repo signing-parity guard
(`tests/test_contract_signing.py` + `tests/test_contract_parity.py`). Suite:
**128 passed** (+7).

**Step 25.4 session A (multi-modal: IMAGE ingest) landed here 2026-06-21 [x-repo].**
A region ingests images (`.png/.jpg/.jpeg/.webp/.gif`) by **captioning** them
into indexable text that flows through the normal chunk→embed→graph path
(architecture §8: project into the *one* fleet-pinned text space; CLIP-style
modality-native vectors stay region-local, out of scope). New
`src/syncsage/ingestion/captioner.py`: `StubCaptioner` (deterministic, offline,
**default** — caption = template over file name + blake2b digest of bytes; an
authored `<image>.caption.txt` sidecar wins) + `OpenAISpecVisionCaptioner`
(gated — OpenAI-spec `chat/completions` with an `image_url` part). Captioning is
the only sanctioned indexing-path network call besides the 21.4 embedder and
keeps the stub path (tests network-free). Config
`ingestion.captioner.{provider,model,base_url,api_key_env,prompt}`
(`stub`|`openai-spec`); the captioner is **only built when a source's `include`
globs admit an image extension**, so a text-only / standalone region is
byte-identical to pre-25.4. **Idempotent:** the engine's pre-read sha256 skip
never re-captions an unchanged image (zero-work, like 21.4's zero re-embed). The
21.5 publisher's `_capabilities()` appends `"image"` to `capabilities.modalities`
when an image source is configured, so the router's `--modality image` filter
(22.1) routes only to image-capable regions. **`modalities` is existing contract
data → wire format / vendored schema UNCHANGED** (no schema bump, no re-vendor,
parity test green). Tiny PNG fixture +
`tests/fixtures/sample_workspace/images/diagram.png.caption.txt`;
`tests/test_image_ingestion.py`. Suite: **135 passed** (+7).

**Step 25.4 session B (multi-modal: AUDIO ingest) landed here 2026-06-21 [x-repo] —
COMPLETES Step 25.4 and Phase 25.** A region ingests audio
(`.wav/.mp3/.m4a/.flac/.ogg`) by **transcribing** it into indexable text that
flows through the normal chunk→embed→graph path — the audio twin of session A's
image captioning (architecture §8: project into the *one* fleet-pinned text
space). New `src/syncsage/ingestion/transcriber.py`: `StubTranscriber`
(deterministic, offline, **default** — transcript = template over file name +
blake2b digest of bytes; an authored `<audio>.transcript.txt` sidecar wins;
**no network / no audio decoder / no ASR model / no audio library**) +
`OpenAISpecTranscriber` (gated — OpenAI-spec `POST {base_url}/audio/transcriptions`
stdlib-urllib multipart upload, transcript from response `text`). A tiny additive
helper `src/syncsage/ingestion/_modal.py` (`sidecar_text` + `stub_fingerprint`)
now backs both the captioner and the transcriber (session A behavior unchanged).
Config `ingestion.transcriber.{provider,model,base_url,api_key_env}`
(`stub`|`openai-spec`, default `whisper-1`); the transcriber is **only built
when a source's `include` globs admit an audio extension**, so a text-only /
standalone region is byte-identical to pre-25.4. **Idempotent:** the engine's
pre-read sha256 skip never re-transcribes an unchanged audio file (zero-work,
like 21.4's zero re-embed). The 21.5 publisher's `_capabilities()` appends
`"audio"` to `capabilities.modalities` when an audio source is configured, so the
router's `--modality audio` filter (22.1) routes only to audio-capable regions.
**`modalities` is existing contract data → wire format / vendored schema
UNCHANGED** (no schema bump, no re-vendor, parity test green). Tiny WAV fixture +
`tests/fixtures/sample_workspace/audio/briefing.wav.transcript.txt`;
`tests/test_audio_ingestion.py`. Suite: **142 passed** (+7). **Phase 25 complete.**

**Step 33.1 (agent memory as a region) landed here 2026-07-16.** Memory
records are **source content**: `memory/store.py` appends one frontmatter
Markdown file per record (`schema_version: 1`, deterministic
`mem-<instant>-<blake2b8>` ids, append-only, `<scope>/` dirs) into a new
built-in `SourceType.memory` filesystem source; indexing is the normal
deterministic pipeline (no second path, no LLM). Write surfaces
(additive): MCP `memory_write` (`SyncSageTools` + server tool) and
`POST/GET /memory` — `sync=true` default → read-your-writes via ordinary
`search_context`; recall IS search. Publisher advertises `"memory"` in
`capabilities.modalities` (25.4 precedent — wire format unchanged, parity
green). Acceptance: `tests/test_memory_region.py`. Docs:
`docs/how-to/agent-memory.md`, contracts in SR `PRODUCT_FRAMEWORK.md` §3c.

**Step 33.2 (memory validity + consolidation) landed here 2026-07-16.**
Supersedes chains resolve across scopes (`list_records(current_only=True)`
/ `GET /memory?current_only=true` filter live). Consolidation is a pure
content operation: superseded + per-scope-TTL-expired records are archived
(`<id>.md.archived` rename in place — bytes preserved, never deleted — so
the `**/*.md` include glob stops matching), then a **full** re-sync of the
small memory source prunes them from index/graph/vectors through the
ordinary pipeline (incremental never prunes). Runs on the 21.1 scheduler
beat (`memory/maintenance.run_memory_maintenance`) + on demand
(`memory_consolidate` MCP tool, `POST /memory/consolidate`). New config
block `memory.{consolidation_enabled, session_ttl_days, user_ttl_days,
org_ttl_days}` (TTLs opt-in `None`, consolidation on by default).
Deterministic in `now`, idempotent second pass.
`tests/test_memory_region.py` (15 total).

**Step 33.4 (memory-recall benchmark) landed here 2026-07-16 — Phase 33
complete.** `memory/benchmark.py` (`python -m syncsage.memory.benchmark`):
LongMemEval-style, deterministic, offline, through the real
`memory_write`→index→`search_context` path. Recorded: recall@5 **1.000**,
update_accuracy **1.000**, stale_leak **0.000**, abstention **1.000**
(30/120/10/10, k=5; canonical numbers in SR `docs/RESULTS.md` §9d). The
bench exposed + drove **two self-search fixes** in
`search/sqlite_store.py`: (1) NL questions zeroed out on FTS5
implicit-AND → MATCH is now an OR of sanitized `_query_tokens` ranked by
BM25; (2) `1/(1+|bm25|)` inverted relevance in hybrid merges → monotone
mapping (LIKE-fallback rows keep 1.0). Gate:
`tests/test_memory_benchmark.py`. Suite: **202 passed** (+4).

**Steps 32.1+32.2+32.6 (ACL persistence, principal filtering, leak gate)
landed here 2026-07-17 [x-repo] — Phase 32 core.** Artifacts gain an `acl`
column (one-shot idempotent additive migration in `StateStore.migrate`;
NULL = pre-32 semantics). `security/acl.py`: per-connector `normalize_acl`
→ canonical `{"allow": ["user:…","group:…"], "public": bool}` (notion
creators, gdrive owners, slack privacy, confluence space+creator, imap
from/to/cc, canonical passthrough otherwise; unreadable ACLs fail closed);
the engine stores it on every indexed artifact. `search_context` accepts
`principal`/`principal_groups` and, under `security.acl_enforced: true`
(**default false — pre-32 byte-identical**), over-fetches candidates and
filters against artifact ACLs BEFORE merge/return (graph nodes without an
artifact row conservatively denied; un-ACL'd artifacts follow
`security.default_visibility`, `groups:` config maps principals→groups —
IdP sync loop = 32.4). Threaded through MCP `search_context` + HTTP
`/search` (router forwards the principal; the region enforces). Leak gate:
`tests/test_acl_enforcement.py` (adversarial cross-user, anonymous
public-only, group via param + config, enforcement-off parity,
normalization rules, fail-closed). Suite: **253 passed** (+5). Router-side
32.3 + deferred 32.4/32.5 live in SR (`PRODUCT_FRAMEWORK.md` §3d).

**Step 32.4 (external-IdP group sync + staleness SLA) landed here 2026-07-18
[x-repo] — completes the SyncSage side of Phase 32.** The 32.2 config-mapped
`security.groups` stays the deterministic core; `security/idp.py` adds a
*synced* principal→groups mapping from a SCIM 2.0 `/Groups` directory
(`fetch_scim_groups` — paginated ListResponse, token from
`security.idp.api_key_env`, one monkeypatch-friendly module-level HTTP fn,
stdlib urllib, zero new deps). Persistence is SQLite: additive `idp_groups` +
`idp_sync_meta` tables in `StateStore.migrate` (CREATE IF NOT EXISTS —
idempotent, user data preserved); `replace_idp_groups` is transactional and
row-stable on an unchanged directory while `synced_at` bumps every successful
pass (the heartbeat IS the SLA clock). Enforcement: the `search_context` ACL
path unions **fresh** IdP groups into the identity set; the **staleness SLA
fails closed** — a mapping older than `security.idp.staleness_max_minutes`
(default 1440) grants NOTHING until the next successful sync (config +
param groups unaffected). Refresh rides the 21.1 scheduler beat
(`run_idp_maintenance` — due-interval check, fetch failures reported never
raised, so the beat survives and the mapping just ages toward the SLA) +
on-demand `POST /security/idp/sync` and `GET /security/idp/status`. Config
`security.idp.{enabled=false, provider=scim, base_url, api_key_env,
sync_interval_minutes=60, staleness_max_minutes=1440}` — **disabled by
default: byte-identical 32.2 behavior**. Docs: `docs/security.md`.
Acceptance: `tests/test_idp_sync.py` (7 — paginated fetch, idempotent
re-sync + heartbeat, fresh-grant vs stale-fail-closed e2e, maintenance due
logic + error resilience, HTTP round-trip, disabled no-ops). Suite:
**260 passed / 2 skipped** (+7). Router-side 32.5 (OIDC bearer→principal +
audit) lands in SR the same day — **Phase 32 complete**.

**Steps 31.3–31.7 (GDrive/Slack/Confluence/IMAP + certification) landed
here 2026-07-16 — Phase 31 complete.** Four more first-party SDK plugins in
`src/syncsage/connectors/` (entry points in pyproject; zero new deps —
stdlib urllib/imaplib, bs4 already core): version-proxy sha256 pre-read
skips, per-item incremental cursors (imap = exact UID high-watermark, a
second sync lists nothing), deterministic rendering, `connector.api_key_env`
secrets, Phase-32 ACL capture. 31.7: certified-connectors table + recipe in
`docs/reference/connector-sdk.md`; the example package now ships the
certification test (`tests/fixtures/syncsage-connector-example/tests/`),
fixture suites excluded via pytest `norecursedirs`.
`tests/test_saas_connectors.py` (34). Suite: **248 passed** (+34).

**Step 31.2 (Notion connector) landed here 2026-07-16.** First-party SDK
plugin dogfooding 31.1: `src/syncsage/connectors/notion.py` under the
`syncsage.connectors` entry-point group in this repo's own `pyproject.toml`
(config `type: notion`, zero new dispatch code). Paginated `POST /v1/search`
listing; block tree → deterministic Markdown (nested to depth 3, no LLM);
`item.sha256` = `(page_id, last_edited_time)` proxy → pre-read skip;
per-page edit-time cursor → `read_item` `ItemNotModified` on incremental;
token via new generic `sources[].connector.api_key_env` (default
`NOTION_TOKEN`); `metadata["acl"]` carries created_by/last_edited_by
(Phase 32 reserved). Offline recorded fixtures `tests/fixtures/notion/`;
`tests/test_notion_connector.py` (12) incl. engine-e2e idempotent +
incremental (zero block fetches on unchanged) + conformance + entry-point
guard. Suite: **214 passed** (+12).

**Step 31.1 (Connector SDK) landed here 2026-07-15.** Third-party connector
plugins resolve by `sources[].type` name via `importlib.metadata` entry
points (group `syncsage.connectors`, `sync/connector_registry.py`) or
programmatic `register_connector_class`; unknown config type strings load
as `PluginSourceType` (a `str` with a `.value` property — existing
`source.type.value` call sites untouched, no workspace anchoring) and
resolve at dispatch (`connector_for_source` falls through to the registry
after the hardcoded built-ins, so the zero-plugin path is byte-identical;
missing plugin → error naming type + installed plugins). Public quality
bar: `syncsage.testing.ConnectorConformance` (subclass + one
`make_connector` factory) — FilesystemConnector passes the same harness.
Canonical third-party shape: `tests/fixtures/syncsage-connector-example/`
(`StaticDirConnector`), engine-e2e + idempotent second sync in
`tests/test_connector_sdk.py`. Docs: `docs/reference/connector-sdk.md`.
Suite: **183 passed** (+19). Steps 31.2–31.6 (Notion/GDrive/Slack/
Confluence/IMAP) build on this, one per session; contracts in the SR
repo's `docs/PRODUCT_FRAMEWORK.md` §3.

**Retrieval + graph overhaul landed 2026-08-03.** Driven by a concrete
failure: asking the demo corpus to "locate readme" returned Python source
while the repository's own `README.md` sat at rank 125.

*Ranking* (`search/sqlite_store.py`, `search/hybrid.py`): query expansion now
drops framing stopwords (a rare verb like "locate" — 15 chunks — outscored
"readme" — 724 — because BM25 weights by rarity); `chunks_fts.title` holds the
**basename** instead of a second copy of `path`, with BM25 column weights
`8/3/2/1`; structural priors divide the score by path depth and by
tests/samples membership; and hybrid merges by **Reciprocal Rank Fusion**
instead of raw score — the three arms scored on incomparable scales (text
0.86-0.92, vector 0.667-0.674, graph a flat 0.60), so hybrid had silently
degraded to text-only while paying for all three. Measured MRR 0.230 → 0.594
on the live 2,132-file corpus. One-shot idempotent FTS rebuild in
`StateStore._migrate_fts_titles` — no re-index needed.

*Concept extraction retired* (`graph.enrichment._add_concept`, now a no-op
with the measurements in its docstring). It was 87.2% of nodes and 98.6% of
edges and failed every test: the retrieval expansion path never fired, the
facts panel filled with "limit"/"request info", and `similar_to` emitted zero
edges. Result: sync 1.5h → **2m53s**, graph 915 MB → 2.3 MB, DB 1.4 GB → 321 MB.
The Synapse contract's vocabulary moved to `fts5vocab` with the **wire format
unchanged** — see the 2026-08-03 decision note in `docs/SYNAPSE_INTEGRATION.md`.

*Internal reference resolution*: `resolve_cross_source_edges` no longer skips
same-source targets, so imports/links resolve to the **file** they name
(2,903 edges on the demo corpus). This is the file→file connectivity the graph
advertised and never had.

*Assistant*: answers are built from whole files reassembled from their chunks
with metadata (`retrieval.documents`), not 500-char previews; questions are
classified `knowledge` vs `procedural` and drive retrieval breadth-vs-depth,
the sufficiency bar and the answering prompt; workflows are
`knowledge-summary | agentic | simple`. Nested `workflow_options` (documented
and previously ignored outright) now works.

Full step contracts with acceptance criteria: `docs/SYNAPSE_INTEGRATION.md` §2.

| Step | What | Fixes | Status |
|---|---|---|---|
| 21.1 | Real watcher + scheduler (watchdog + interval loop) | stub `sync/watcher.py`, `sync/scheduler.py` | done (2026-06-10) |
| 21.2 | WAL + single-writer lease + manifests→SQLite | crash/concurrency safety; loose-JSON manifests | done (2026-06-10) |
| 21.3 | True incremental web/API/S3 (ETag/cursor/watermark) | connectors ignore their checkpoints | done (2026-06-10) |
| 21.4 | Per-region vector index (lancedb, `[vector]` extra) + OpenAI-spec embedder, `mode="vector"` in hybrid search **[x-repo]** | dead `search.embeddings`/`vector_store` config | done (2026-06-13) |
| 21.5 | Semantic-contract publisher + NDJSON event stream + router webhook **[x-repo]** | no sync-completion signal; no contract | done (2026-06-14) |
| 21.6 | Graph snapshots (zstd) + retention + backup/restore; cross-source edges + concept normalization | dead storage config; no backup; no cross-source links | done (session A 2026-06-18; session B 2026-06-18) — Phase 21 complete |

---

## 6. Pointers

- **Region-side Synapse spec:** `docs/SYNAPSE_INTEGRATION.md`
- **System architecture + framework (other repo):**
  `subjective-retrieval/docs/SYNAPSE_ARCHITECTURE.md`,
  `…/docs/SYNAPSE_FRAMEWORK.md`, ADR 2026-06-10 in `…/docs/DECISIONS.md`
- **Graph taxonomy:** `docs/graph_model.md` · **Config:** `docs/configuration.md`
- **MCP:** `docs/mcp_tools.md`, `docs/mcp_client.md`
- **Deployment:** `docs/deployment.md`, `deploy/kubernetes/`

If docs drift from code, **the code is authoritative** — flag the drift in
`docs/SYNAPSE_INTEGRATION.md` (Synapse scope) or the relevant doc.
