# CLAUDE.md — pheasant

Primary context hand-off for any Claude agent working on this repository.
Read it first; it is intentionally dense. Anything not here should be
derivable from the code, `docs/`, or a single grep.

---

## 1. What this project is

**pheasant** is a Docker-first, local-first **MCP context server** that
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

### 1.1 pheasant's second role: a Synapse brain region

Since 2026-06-10, pheasant is also the **region** component of
**Synapse** — a federated knowledge-base system whose router (the
"nervous system") lives in the sibling **pheasant-flock** repo.
Each pheasant container publishes a **semantic contract** derived from
its own content; the router scores contracts to decide which regions to
query and fans out to each region's self-search. Read
`docs/SYNAPSE_INTEGRATION.md` before doing any Synapse-related work here.
Two iron rules: (1) the contract schema is canonical in
pheasant-flock — this repo only vendors the exported JSON Schema +
fixtures under `contracts/`; (2) **no Python dependency between the
repos** — the boundary is contract JSON + HTTP, and a router-less
pheasant must keep working unchanged.

> **The router repo was renamed 2026-08-06**: `subjective-retrieval` →
> **pheasant-flock** (import root `pheasant_flock`, CLI `pflock`). Nothing in
> this repo's own code changed — only references to the sibling. That rename
> *did* move the vendored contract bytes for the first time: the schema's
> `$id` carries the router's repo URL, so `contracts/semantic_contract.v1.schema.json`
> and `contracts/PARITY.json` were re-vendored. **The wire format is
> unchanged** — contract instances never embed `$id`, so both fixtures stayed
> byte-identical and the signed one still verifies. The sibling checkout the
> parity test looks for is now `../pheasant-flock`.

---

## 2. Repository layout

```
pheasant/
├── CLAUDE.md                  ← you are here
├── README.md
├── pyproject.toml             ← extras: [mcp], [dev]; (Phase 21.4 adds [vector]);
│                                core deps include numpy + zstandard (21.6A snapshots/backup)
├── pheasant.example.yaml      ← reference config (all sections)
├── Dockerfile                 ← python:3.12-slim, port 8765, /config/pheasant.yaml
├── docker-compose.yml         ← pheasant + optional pheasant-ui sidecar
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
├── src/pheasant/
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
│   ├── mcp_server/            ← server.py (FastMCP), tools.py (PheasantTools),
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

Key entities: **knowledge base** (`kb_id` = `pheasant.name`) → **sources**
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

pheasant up [PATH] [--no-serve]            # zero-config quickstart: detect
                                           #   (.obsidian/.git/folder) → generate
                                           #   config → index → serve (Step 30.1)
pheasant init --profile quickstart         # generate starter pheasant.yaml
pheasant validate && pheasant doctor       # config + environment checks
pheasant start                             # HTTP API + MCP on :8765
pheasant sync --source <name> --mode incremental|full|validate_only|repair
pheasant mcp --transport stdio             # standalone MCP server
pheasant client-config claude-code|cursor|vscode   # emit MCP client config
                                           #   (agents: --mode local|docker-exec|
                                           #    docker-run; Step 30.5)
pheasant config show                       # resolved config after profile+YAML+--set
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
6. **Never import pheasant-flock.** The Synapse boundary is
   contract JSON + HTTP (see §1.1). Never hand-edit vendored files under
   `contracts/`.
7. **Standalone mode is sacred.** Every change must leave a router-less
   pheasant fully functional; Synapse features no-op when
   `synapse.router_url` is unset.
8. **MCP tool surface is public API.** Renaming/removing tools in
   `mcp_server/tools.py` breaks deployed agents — additive evolution only,
   deprecate before remove.
9. **Cross-repo work** (anything marked [x-repo] in
   `docs/SYNAPSE_INTEGRATION.md`) follows the `pheasant-coordinator`
   skill in the pheasant-flock repo: identical branch names in both
   repos, contract fixture parity (sha256), both test suites green before
   either push.
10. **Scope each session to one Phase-21 step** (or one bugfix). Write
    `runs/<ts>-synapse-<step>/SUMMARY.md` per the framework conventions.

---

## 5. Current Synapse work queue (Phase 21 — region hardening)

**Phase 21 region hardening is complete (2026-06-18).** Remaining Synapse work
lives in the sibling pheasant-flock repo (Phase 22 finish — Step 22.5 —
plus Phases 23–26).

**Step 24.4 (A2A + Ed25519-signed contracts) landed here 2026-06-20 [x-repo].**
The publisher now **optionally signs** the contract's `integrity.signature`
(Ed25519) when `synapse.signing_key_ref` is set (a secret *reference* —
`env://NAME` or a bare env var name — resolving to a base64 32-byte Ed25519
seed; the plaintext key never lands in config or on disk). Unset → unsigned,
`integrity.signature: null`, and a **standalone pheasant is unchanged**. New
`src/pheasant/synapse/signing.py` (`sign_body`/`signing_bytes`/`content_hash`/
`resolve_signing_key_ref`/`public_key_b64`); the `cryptography` import is gated
behind the new optional **`[a2a]` extra** (a region without `signing_key_ref`
needs no crypto dep; offline tests `importorskip("cryptography")`). The
signature covers byte-identical canonical body bytes to `_content_hash`, so the
router's `SemanticContract.verify_signature` accepts it — **out-of-band public
key** decision: the router carries the kb_id→pubkey trust store in *its* config,
so the contract wire format / vendored schema are **unchanged** (no re-vendor;
schema export asserted byte-identical in Flock). A new vendored
`contracts/fixtures/signed-demo-region.v1.contract.json` (+ PARITY.json sha256
both repos) is the cross-repo signing-parity guard
(`tests/test_contract_signing.py` + `tests/test_contract_parity.py`). Suite:
**128 passed** (+7).

**Step 25.4 session A (multi-modal: IMAGE ingest) landed here 2026-06-21 [x-repo].**
A region ingests images (`.png/.jpg/.jpeg/.webp/.gif`) by **captioning** them
into indexable text that flows through the normal chunk→embed→graph path
(architecture §8: project into the *one* fleet-pinned text space; CLIP-style
modality-native vectors stay region-local, out of scope). New
`src/pheasant/ingestion/captioner.py`: `StubCaptioner` (deterministic, offline,
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
space). New `src/pheasant/ingestion/transcriber.py`: `StubTranscriber`
(deterministic, offline, **default** — transcript = template over file name +
blake2b digest of bytes; an authored `<audio>.transcript.txt` sidecar wins;
**no network / no audio decoder / no ASR model / no audio library**) +
`OpenAISpecTranscriber` (gated — OpenAI-spec `POST {base_url}/audio/transcriptions`
stdlib-urllib multipart upload, transcript from response `text`). A tiny additive
helper `src/pheasant/ingestion/_modal.py` (`sidecar_text` + `stub_fingerprint`)
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
(additive): MCP `memory_write` (`PheasantTools` + server tool) and
`POST/GET /memory` — `sync=true` default → read-your-writes via ordinary
`search_context`; recall IS search. Publisher advertises `"memory"` in
`capabilities.modalities` (25.4 precedent — wire format unchanged, parity
green). Acceptance: `tests/test_memory_region.py`. Docs:
`docs/how-to/agent-memory.md`, contracts in Flock `PRODUCT_FRAMEWORK.md` §3c.

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
complete.** `memory/benchmark.py` (`python -m pheasant.memory.benchmark`):
LongMemEval-style, deterministic, offline, through the real
`memory_write`→index→`search_context` path. Recorded: recall@5 **1.000**,
update_accuracy **1.000**, stale_leak **0.000**, abstention **1.000**
(30/120/10/10, k=5; canonical numbers in Flock `docs/RESULTS.md` §9d). The
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
32.3 + deferred 32.4/32.5 live in Flock (`PRODUCT_FRAMEWORK.md` §3d).

**Step 32.4 (external-IdP group sync + staleness SLA) landed here 2026-07-18
[x-repo] — completes the pheasant side of Phase 32.** The 32.2 config-mapped
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
audit) lands in Flock the same day — **Phase 32 complete**.

**Steps 31.3–31.7 (GDrive/Slack/Confluence/IMAP + certification) landed
here 2026-07-16 — Phase 31 complete.** Four more first-party SDK plugins in
`src/pheasant/connectors/` (entry points in pyproject; zero new deps —
stdlib urllib/imaplib, bs4 already core): version-proxy sha256 pre-read
skips, per-item incremental cursors (imap = exact UID high-watermark, a
second sync lists nothing), deterministic rendering, `connector.api_key_env`
secrets, Phase-32 ACL capture. 31.7: certified-connectors table + recipe in
`docs/reference/connector-sdk.md`; the example package now ships the
certification test (`tests/fixtures/pheasant-connector-example/tests/`),
fixture suites excluded via pytest `norecursedirs`.
`tests/test_saas_connectors.py` (34). Suite: **248 passed** (+34).

**Step 31.2 (Notion connector) landed here 2026-07-16.** First-party SDK
plugin dogfooding 31.1: `src/pheasant/connectors/notion.py` under the
`pheasant.connectors` entry-point group in this repo's own `pyproject.toml`
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
points (group `pheasant.connectors`, `sync/connector_registry.py`) or
programmatic `register_connector_class`; unknown config type strings load
as `PluginSourceType` (a `str` with a `.value` property — existing
`source.type.value` call sites untouched, no workspace anchoring) and
resolve at dispatch (`connector_for_source` falls through to the registry
after the hardcoded built-ins, so the zero-plugin path is byte-identical;
missing plugin → error naming type + installed plugins). Public quality
bar: `pheasant.testing.ConnectorConformance` (subclass + one
`make_connector` factory) — FilesystemConnector passes the same harness.
Canonical third-party shape: `tests/fixtures/pheasant-connector-example/`
(`StaticDirConnector`), engine-e2e + idempotent second sync in
`tests/test_connector_sdk.py`. Docs: `docs/reference/connector-sdk.md`.
Suite: **183 passed** (+19). Steps 31.2–31.6 (Notion/GDrive/Slack/
Confluence/IMAP) build on this, one per session; contracts in the Flock
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

**Phase 34 (WASM sandboxing & selective acceleration) queued 2026-08-03.**
Full step contracts in `docs/SYNAPSE_INTEGRATION.md` §5. Not
Synapse-contract work — no wire-format impact, no `[x-repo]` obligation.
Anchored on two confirmed, unmitigated gaps found by reading the hot paths
directly (judged against what's left *after* the 2026-08-03 concept-extraction
retirement above, not against that already-solved cost): (1) third-party
connector plugins run fully-trusted, unsandboxed Python in-process
(`sync/connector_registry.py`'s `ep.load()`, ambient secrets via
`os.environ`); (2) two O(graph) hot loops that scale with multi-source
growth — `graph/builder.py:add_cross_source_edges` (every sync, can't be
`changed_ids`-gated like `add_similarity_edges` because references need both
sides indexed) and `search/graph_search.py:_scan_edges` (every relationship
query, no FTS5-style prefilter unlike node search). Sandboxing
(`wasmtime`/WASI, new `[wasm]` extra, opt-in `connector.runtime: sandboxed`
tier alongside native) and speed work (WASM-accelerated cross-source
resolution + relationship search, gated on a benchmark spike) are independent
tracks; mono-container-per-KB architecture is unchanged. Steps 34.1-34.7, one
per session, `runs/<ts>-synapse-34.N/SUMMARY.md` each.

**Step 34.1 (host harness) landed here 2026-08-03.** New
`src/pheasant/sandbox/wasm_runtime.py`: `WasmSandbox` wraps one `wasmtime`
guest instance — deterministic fuel metering (`Config.consume_fuel` +
`Store.set_fuel`, not a wall-clock timeout), a per-instance linear-memory cap
(`Store.set_limits`), and a capability-scoped `host_fetch_len`/
`host_fetch_read` import pair gated by `HostCapabilities.allowed_hosts` (no
ambient WASI env/fs/net; reuses `sync/connectors.py`'s `require_fetchable_url`
rather than reimplementing scheme checks). Traps translate to typed
exceptions (`SandboxFuelExhausted`, `SandboxMemoryLimitExceeded`,
`SandboxCapabilityDenied`, `SandboxTrapped`); a region without the new
`[wasm]` extra (`wasmtime>=20`) stays byte-identical
(`WasmRuntimeUnavailable` raised only on actual use, never on import).
**Toolchain note:** no Rust/TinyGo toolchain was available in this
environment, so guest fixtures are hand-authored WAT text compiled by
`wasmtime.Module`'s built-in WAT parser — no external compiler needed for
the reference/test guests; a real toolchain is still expected for
third-party connector authors (unchanged from the plan).
`tests/test_wasm_harness.py` (6 tests): hello-wasm runs, fuel exhaustion and
memory-cap overrun both fail closed, a `host_fetch` round trip succeeds for
an allowlisted host and is denied otherwise, gated-import guarantee holds.
Suite: **516 passed, 7 skipped** (+6).

**Step 34.2 (reference sandboxed connector) landed here 2026-08-03.** New
`src/pheasant/sandbox/connector.py`: `SandboxedConnector` ports
`StaticDirConnector`'s shape — lists/reads `*.txt` files host-side (guarded
by `security/path_policy.resolve_under`, reused not duplicated) and runs
each file's bytes through a bundled WASM guest's `normalize()` export
(CRLF→LF) inside the 34.1 harness before hashing/storing.
`connector_for_source` (`sync/connectors.py`) now checks
`source.connector.runtime == "sandboxed"` before its `source.type` dispatch
(new `SourceConnectorSettings.{runtime, allowed_hosts, wasm_module_path}`,
all additive, `runtime` default `"native"` — an untouched source is
byte-identical to pre-34.1). **Deviation from the plan text:** listing/read
stays host-side rather than the guest doing its own WASI
`fd_readdir`/`path_open` syscalls — hand-authoring WASI preview1's
filesystem ABI in raw WAT (no Rust/TinyGo toolchain available, per 34.1) was
judged too easy to get subtly wrong without a way to validate it; the guest
still processes real untrusted per-item bytes under the fuel/memory cap,
which is the actual threat-model target (Confluence's in-process
BeautifulSoup parse of untrusted remote XHTML is the named example) — full
rationale in the run summary. `pheasant.testing.ConnectorConformance`
needed **no code changes** — it was already connector-agnostic, so the
sandboxed connector gets the identical bar for free via a new subclass.
`tests/test_sandboxed_connector.py` (9 tests, +9). Suite: **525 passed, 7
skipped**.

**Step 34.3 (adversarial limit enforcement) landed here 2026-08-03.**
`WasmSandbox.__init__` now wraps `linker.instantiate`: a guest declaring an
import the sandbox never wires (e.g. `wasi_snapshot_preview1.environ_get` —
no ambient WASI env is ever provided) fails to **load at all**, surfaced as
`SandboxCapabilityDenied` rather than a raw `wasmtime.WasmtimeError`.
`tests/test_wasm_adversarial.py` (5 tests) proves all four named adversarial
shapes fail closed with the specific properties the step calls for: unbounded
memory / infinite loop both raise their typed exception inside a wall-clock
bound (no hang); ambient env read is denied before any guest code runs; two
SSRF-shaped host_fetch attempts (`file:///etc/passwd` scheme disguise, a
`169.254.169.254` metadata-endpoint host) are both denied with the fetcher
asserted **never invoked** (no secret leak) and the guest's result buffer
staying all-zero (no partial write). New fixtures:
`tests/fixtures/wasm/{wasi_env_leak,host_fetch_ssrf}.wat`. Suite: **530
passed, 7 skipped** (+5). **Scope note:** the plan's broader manual
end-to-end check (malicious connector wired into a live `pheasant sync`,
confirming the parent API server keeps serving via `sync/worker.py`'s
pre-existing child-process isolation) is left as a documented follow-up —
34.3's own acceptance is fully covered by the automated suite, and that
check exercises Phase 21.1 process isolation rather than anything new here.
This **closes the sandboxing arc (34.1-34.3)**. Next: Step 34.4 (benchmark
spike — no production code expected, numbers only).

**Live validation against the real demo-agent-framework corpus, 2026-08-04.**
Rebuilt `examples/demo-agent-framework/` with `PHEASANT_EXTRAS=mcp,agent,
wasm` and both 34.5 flags on, ran a real full sync (2,132 files → 22,683
nodes / 54,406 edges). Found and fixed a real bug in the process: `graph:`
config sections were silently discarded entirely —
`PheasantConfig.model_validate`'s constructor call was missing
`graph=build(GraphSettings, data.get("graph"))`, so `concept_min_documents`
was ALSO never configurable via YAML, not just the new WASM flag; no test
caught it because none exercised the `graph:` section. Fixed in
`config/schema.py`; regression test in `tests/test_config_loading.py`.
Both accelerators then confirmed correct on the real corpus (WASM AOT
cache file appeared exactly at sync completion; a direct in-container
check found `_scan_edges` producing identical hits — 10,861 — between
Python and WASM). **Revised finding:** real-world `_scan_edges` speedup was
only ~1.13x (1.278s → 1.130s), far below the 34.4 synthetic benchmark's
~5x at comparable edge counts — the synthetic fixture's short placeholder
strings understated real marshal cost; the scaling *shape* held, the
*magnitude* did not transfer. Full detail:
`runs/2026-08-04-synapse-34-live-validation/SUMMARY.md`.

**Step 34.4 (benchmark spike) partially landed here 2026-08-03 — blocked on
a toolchain decision.** `runs/2026-08-03-synapse-34.4/benchmark.py` measures
the pure-Python baseline for both named hot loops at 6 scale points each:
`resolve_cross_source_edges` is confirmed genuinely O(V+E) (flat ~5-6
μs/edge, 2→64 sources) when relative paths are globally unique, but a
previously-undocumented **O(sources²)** degenerate case was found when N
sources share the same relative-path layout (a single `by_path` bucket fans
out to N candidates — `enrichment.py:346-354`, not a WASM problem, an
algorithmic fix if ever addressed). `graph_search._scan_edges` is confirmed
O(edges) (~10-14 μs/edge, 500→16,000 edges) but lands on the **query path**
with no index prefilter — ~225ms at 16k edges, extrapolating to ~2.2s at
160k, the stronger practical case for acceleration. **No WASM comparison arm
was produced**: a faithful port needs a hash-map-backed path index +
Python-identical string-matching rules (far more code than the 34.1-34.3
fixtures), and this repo still has no Rust/TinyGo toolchain — hand-authoring
that logic in raw WAT risks a silent wrong-answer bug (unlike a sandboxing
fixture, there's no fail-closed trap to catch it), so it was not attempted
without a toolchain decision. User chose: install a real toolchain. `rustup` (GNU host, no MSVC
dependency) + `wasm32-unknown-unknown` installed and verified end-to-end
(smoke-test crate: Rust → wasm → wasmtime → correct result) before writing
the real port.

**Step 34.4 completed here 2026-08-03 (for (a)/(b) — (c)/(d) deferred per
the plan's own priority order).** `runs/2026-08-03-synapse-34.4/wasm_bench/`
(a spike-only Rust crate, not shipped under `src/pheasant`) ports
`resolve_cross_source_edges`'s `python_import` path and the full
`_scan_edges` scoring logic to `wasm32-unknown-unknown`. Every one of 12
tested scale points asserts the WASM output is byte-for-byte identical to
the Python reference before any timing counts — correctness parity-verified,
not assumed. **Finding #1 (implementation-critical):** a naive
per-call-compiled sandbox makes WASM 5-20x *slower* than Python at every
scale (`WasmSandbox.__init__` JIT-compiles the module every construction) —
any real integration must compile the module once at startup and reuse it;
a steady-state harness (module precompiled, fresh `Store`/`Instance` per
call) is the number that actually matters. **Finding #2 — the two loops
diverge sharply:** `_scan_edges` steady-state WASM is a consistent, growing
2-8x win at every scale tested (500→64,000 edges) — highest-confidence part
of the spike, on the query-latency-sensitive function with no existing
mitigation. `resolve_cross_source_edges` steady-state WASM **loses to
Python below ~1,300-2,500 edges** and only starts winning above that — and
today's actual demo corpus (2,903 cross-source edges, per this file's
2026-08-03 retrieval-overhaul entry) sits almost exactly at that breakeven
point. **Go/no-go: 34.5b is a GO; 34.5a is a CONDITIONAL GO** (recommend
pairing with a marshal-format optimization — string-interning repeated
paths/source-ids instead of repeating them per edge — which would likely
lower the crossover and widen the margin, not just shift it). No production
code changed this step (git-confirmed); benchmark scripts + Rust crate live
under `runs/2026-08-03-synapse-34.4/` (gitignored). Next: Step 34.5,
34.5b prioritized over 34.5a, both compiling their guest module once at
startup per Finding #1.

**Step 34.5 (WASM-accelerated cross-source resolution + relationship
search) landed here 2026-08-03.** Solved Finding #1 (naive per-call
compile) with `wasmtime`'s AOT module serialization: a fresh `Config`+
`Engine`+`deserialize` (fully matching a cold `sync/worker.py` subprocess —
`subprocess.run`, one call per process lifetime, no second call to
amortize an in-process compile against) loads a precompiled artifact in
under 1ms vs. ~103ms to JIT-compile from raw `.wasm` bytes. New
`src/pheasant/sandbox/accel/`: a production Rust crate
(vendored compiled `accel.wasm`, 112KB) + `loader.py` (process-wide
singleton, machine-local `.cwasm` cache under the OS temp dir — deliberately
**not** under any KB's `/state`, since the compiled binary is
knowledge-base-**independent** by design: same binary for every KB, graph
data is a call argument, never baked into the module; the cache is a
build-cache-for-a-generic-binary, not KB state — directly answers the
deployment-genericity question raised before this step). **Correctness
scope note:** 34.5a is a **full** port of
`resolve_cross_source_edges` — both `python_import` *and*
`document_link`/`url` reference-type paths, unlike the 34.4 spike which
only covered the former; shipping a partial port behind a config flag would
have silently broken markdown/document-link cross-source edges, not just
changed performance. A subtle catch during the port: Python's
`str.lstrip("./")` strips a *character set* (every leading `.` or `/`), not
the two-char literal `"./"` once — a naive `trim_start_matches("./")` would
have silently under-stripped `"../../foo.md"`-shaped links; caught by the
parity tests before it shipped. Both accelerators are wired opt-in
(`graph.wasm_cross_source_resolution`, `search.wasm_relationship_search`,
both default **off**) with a broad `except Exception` → pure-Python
fallback + logged warning at every call site — acceleration is a
performance path, never a correctness dependency, verified by dedicated
failure-injection tests (monkeypatch the WASM wrapper to raise, confirm the
end-to-end result is still correct). `tests/test_wasm_accel.py` (8, function
parity incl. edge cases the 34.4 spike never exercised),
`tests/test_wasm_accel_integration.py` (5, flag on/off byte-identical
through a real sync/search + fallback-under-failure),
`tests/test_wasm_accel_loader.py` (4, the AOT cache mechanism itself,
including proving a "fresh process" loads from cache rather than
recompiling by patching `Module.__init__` to raise if called). Suite:
**547 passed, 7 skipped** (+17).

**Steps 34.6 and 34.7 evaluated and closed here 2026-08-03 — both NO-GO,
neither implemented, completing Phase 34.** Ran the missing 34.4c/34.4d
benchmark slices rather than skip straight to implementation:

- **34.4c / 34.6 (chunking):** `chunk_text` runs once per **file**, not
  once per sync — the "small units, called often" shape the plan already
  flagged as weak. Measured: Python chunks a 2 KB file in 35 μs, a 10 KB
  file in 219 μs, a 50 KB file in 2,865 μs; the bare fixed cost of a WASM
  Store+Instance (no marshal, no compute) is 115 μs. WASM loses outright
  on typical small files before doing any work, and only large files leave
  enough headroom to plausibly win — while every file, regardless of size,
  pays the call. No Rust port was written: the numbers already answer it,
  and a faithful port has a real correctness hazard 34.5 didn't (Python's
  `text[start:end]` slices by Unicode code point; a naive byte-oriented
  Rust port would silently mis-chunk any non-ASCII content).
- **34.4d / 34.7 (packed graph representation):** measured live (uncompressed,
  in-process) `SimpleMultiDiGraph` memory via `tracemalloc` at the actual
  demo-corpus scale (2,132 files → 13,503 nodes/13,502 edges) and a 10x
  stress scale (21,320 files → 135K/135K): **20.5 MB and 205 MB**
  respectively. Not a memory problem at either scale by any reasonable
  container budget — the plan's own "2.3 MB compressed" reference
  undersold how small this already is once measured live rather than
  assumed. No implementation attempted; touching `SimpleMultiDiGraph`
  (the core structure the whole indexing/search/enrichment pipeline reads
  and writes) isn't justified without a real constraint to fix.

Both write-ups are full run summaries with the reasoning and numbers, not
just a one-line "skipped" — the plan explicitly wanted the spike's answer
recorded even when the answer is no. **Phase 34 (WASM sandboxing &
selective acceleration) is complete**: 34.1-34.3 shipped (sandboxing),
34.4 shipped (benchmark data + the toolchain), 34.5 shipped (both
accelerated hot loops, production-wired, opt-in), 34.6-34.7 evaluated and
correctly not built.

---

## Dogfooding fallout, 2026-08-04 — background sync, graph UI cleanup, a live crash fix

Not Phase-34 scoped — a follow-up UI/UX + bugfix session prompted by
actually using the demo-agent-framework deployment (adding a second real
source, mlflow, through the UI) rather than only curling the API.

**Sync no longer blocks the request that started it — `wait: false`.**
Adding a source through the UI's quick-add hit a **504** on a repo the size
of mlflow: `POST /sources/quick-add` (and `POST /sources` with
`sync_now`, and `POST /sync/{id}`) ran the sync *inside* the request/
response cycle, so a first index that takes minutes outlives what a
browser tab or reverse proxy holds a connection open for — even though the
sync went on to succeed server-side, the client only ever saw a timeout.
Fixed with a new `wait: bool` field on all three endpoints (default
`true` — the original blocking contract, still what `pheasant up`/CLI
callers and the existing `test_quick_add_registers_and_syncs_a_pasted_path`
test get). `wait: false` hands the sync to a background thread (the same
`sync/worker.py` subprocess `_index` already used, not a new path) and
returns immediately; `app.state.syncing_sources`/`sync_outcomes`
(lock-guarded, process-lifetime, in-memory) track it, surfaced as
`syncing`/`sync_error` on every source in `GET /sources` and `GET
/overview`. The UI's quick-add, "Advanced…" wizard, and both sync buttons
(Sources page + the Notebook sources rail) all set `wait: false` now and
poll (`refetchInterval`, active only while something is syncing) instead
of blocking their own form/button on the result — the reported "stuck at
the form, have to scroll back up to where I was" complaint was really "the
form can't return until a multi-minute sync finishes," which no scroll fix
could have addressed.

**Regression caught by the live demo, not by the unit tests first
written:** the `wait: false` validation checked `source_id` against
`config.sources` only. A source registered at runtime (quick-add) lives in
the state registry immediately but only reaches *that process's*
`config.sources` — `SyncEngine._source` already has a state-registry
fallback for exactly this (a second process, e.g. the sync worker, reading
a source the YAML never mentioned), and the new validation had to check
the same place or a perfectly good source 404s the instant a fresh process
(here: this container after a rebuild/restart) is asked to sync it in the
background. Reproduced with a two-`TestClient`-same-`/state` regression
test before trusting the fix
(`test_sync_source_with_wait_false_accepts_a_source_known_only_to_the_state_registry`).

**A second live-only bug, found once the sync actually ran to completion
on mlflow's real content:** `graph/enrichment.py`'s `_reference_label`
(a cosmetic display label over a reference string — node identity comes
from `_node_id`, unaffected) called `urlparse(value)` unguarded.
`urllib.parse` raises `ValueError: Invalid IPv6 URL` — not a graceful
"can't parse, return unparsed" the way it handles other malformed
input — for a netloc-position string starting with `[` and never closing
it (minimal repro: `"//[foo"`), which crashed the entire sync the moment
mlflow's real markdown produced one. Fixed with a narrow `try/except
ValueError: return value` (fail open to the raw string, exactly what
every other branch of this function already does for "didn't parse as a
URL"). Regression tests at both the unit level and a full `sync_source`
end-to-end call with the crashing shape actually indexed.

**Graph canvas cleanup, reported live as "Kamada-Kawai breaks the UI" +
"entity/external_reference should be hidden by default":** removed the
`kamada-kawai` layout option (ELK's `stress` algorithm via
`cytoscape-elk` — the package's only caller) from `GraphCanvas.tsx` and
`state/session.tsx`'s `GRAPH_LAYOUTS`, and dropped the now-unused
`cytoscape-elk` dependency — regenerating `package-lock.json` via a
throwaway `node:22-alpine` container (no local npm here) and confirming
`npm ci` still resolves cleanly. Side effect: the UI's production JS
bundle shrank from **2,309 kB to 847 kB** (ELK.js is large). Added
`"entity"` and `"external_reference"` to `NOISY_NODE_TYPES` — the existing
default-hidden-legend-types mechanism (`chunk`/`concept` were already in
it) — so both hide by default in a fresh session, matching the other two
noisy-by-volume types already there; a browser with prior localStorage
keeps whatever it already had toggled, which is the correct behavior for
a default change, not a bug.

**Chat scroll-jump fix (from the immediately preceding turn, same
session):** `ChatPanel.tsx`'s post-answer effect scrolled the whole
conversation pane to its absolute bottom on every turn update. For an
answer longer than a screenful this put the viewport at the *end* of the
answer, with the user's own question — and the start of the answer —
scrolled out of view above, needing a manual scroll back up to resume
reading. Replaced with `scrollIntoView({block: "start"})` on the latest
turn's own element (tracked per-turn-id in a `Map` ref, since the same
turn re-renders in place when its answer arrives), pinning the question to
the top of the viewport instead of chasing the bottom of whatever text
just arrived.

Full suite: **554 passed, 7 skipped** (+7 over the Phase 34 total: 4
background-sync tests, 1 state-registry-fallback regression, 2
`_reference_label`/urlparse tests). `tsc -b && vite build` clean. All
changes verified live against the running demo-agent-framework stack
(rebuild → real sync → real 404/500 → fix → rebuild → real success), not
just unit-tested in isolation — both live-only bugs above would not have
been caught by the pre-existing test suite alone.

---

## Document text extraction (PDF/DOCX/HTML), 2026-08-06

Not Phase-scoped — closing a gap found by asking pheasant about its own
codebase. **`.pdf`/`.docx` were accepted by the ingestion pipeline and then
silently produced no text**: `parse_file`/`parse_connector_payload` admitted
both, `artifact_type` labelled them `document`, and `read_text` /
`read_text_bytes` hard-returned `""` — so a PDF got an artifact row, a sha256
and a graph node while contributing **zero chunks**. Findable by path,
invisible by content (verified live: a real text PDF → 0 chars). Two things the
original report missed: **`pymupdf>=1.24` and `python-docx>=1.1` were already
*core* deps in `pyproject.toml`, imported nowhere** — every deployment was
carrying both wheels for nothing; and **HTML/XML is a second, milder gap** —
`.html`/`.xml` sit in `TEXT_EXTENSIONS` and index as *raw markup* (tags,
`<script>`, CSS as prose). `.pptx/.xlsx/.doc/.rtf/.epub` are in no extension
set at all — never accepted, so not a broken promise and out of scope.

New `src/pheasant/ingestion/extractor.py` follows the 25.4 captioner/
transcriber shape exactly (Protocol + provider selection + authored
`<file>.extract.txt` sidecar wins + built **only** when a source's `include`
globs admit `.pdf`/`.docx` + `_modal.py` helper reuse), so this is the third
instance of one pattern, not a new one. It differs in a way that is strictly
*better*: captioning/transcription need a model to invent text that isn't in
the bytes, document extraction doesn't — so **every provider is offline and
deterministic** and rule 1 holds by construction, with no network path to gate.
Providers: `auto` (default — `pymupdf`/`python-docx`, else builtin, keeping
whichever yields text; never raises into a sync), `native`, `builtin`
(**stdlib only** — `zlib` + PDF content-stream operator scan; `zipfile` +
`xml.etree` over `word/document.xml`, which is not a fidelity compromise since
that IS where DOCX text lives), and `sandboxed`. `ingestion.extractor.
{provider, html_text}`; `html_text` defaults **false** because `.html`/`.xml`
have always indexed as raw markup and stripping changes existing chunk
boundaries. Publisher advertises `"document"` in `capabilities.modalities`
(25.4 precedent — **wire format unchanged**, parity green). Idempotent: the
engine's pre-read sha256 skip never re-extracts an unchanged document.
**Flock reuse:** its `corpus/loaders/local_files.py` `_read_pdf` (pdfminer) +
`_read_html` contributed the *shape* — lazy per-format import, graceful
degradation, never raise — not the code (pheasant ships pymupdf, and nothing
was imported across the repo boundary).

**WASM — my initial prediction was wrong, and the measurement says so.** I
expected acceleration to be a NO-GO by analogy to 34.6 (WASM chunking, which
lost to a ~115 µs fixed instance cost). It does not transfer. With the module
AOT-precompiled (34.4 Finding #1), `sandboxed` beats the pure-Python `builtin`
at every size above ~1 KB: **0.50x at 100 lines, 0.30x at 8,000** (129 ms →
38 ms), and the tokenizer alone is **11.4x** (91.8 ms → 8.0 ms on a 585 KB
stream); fixed cost per guest call 0.22 ms, crossover ~30-50 lines. Why 34.6's
logic didn't carry: what matters is **work per call**, not calls per sync —
chunking's 35-219 µs is the same order as the overhead, PDF tokenizing's
1.5-130 ms is 7-600x it. So the sandbox is **not a tax**, and `builtin` vs
`sandboxed` is purely a fidelity/isolation choice. `native` stays the default
regardless: it does full page layout analysis and handles encrypted PDFs,
LZW/CCITT and Type0/CID CMaps that the tokenizer does not. AOT cache
re-verified for this path (JIT 52.8 ms → **0.87 ms**), distinct `extract-`
cache prefix so the trusted accelerator and untrusted-input path never share an
artifact.

Sandboxing is the right framing because 34.2's docstring already named the
target ("Confluence's in-process BeautifulSoup parse of untrusted remote
XHTML") and PDF is its sharper form: connector PDFs (Drive/Slack/Confluence/
IMAP) are attacker-influenced bytes parsed with the sync worker's ambient
authority — every connector token in `os.environ`, writable `/state`, egress.
New `pdf_scan_text` export on the **existing** vendored Rust crate (re-vendored
`accel.wasm`, 112→120 KB; the toolchain reproduces the old binary
byte-identically, sha `f664fa78…`, before any edit) + `ingestion/
extractor_sandbox.py` running it under a fuel cap, memory cap and an **empty
`Linker`** (zero host imports, asserted). Three honest limits, all in the
module docstring: **partial sandbox** (host still inflates via `zlib`, bounded
against bombs; the guest runs the tokenizer, which is where hostile bytes drive
unbounded loops — deliberate split, 34.2-style recorded deviation); **PDF only**
(DOCX/HTML keep memory-safe Python parsers); and it **fails loudly** — missing
`wasmtime` under `provider: sandboxed` raises with a pip hint rather than
silently extracting unsandboxed, inverting 34.5's fallback policy on purpose
because this is a security property, not a performance path (per-*file*
failures still never abort a sync). A sandbox cannot catch a **wrong answer**,
so `pdf_scan_text` is asserted byte-identical to `scan_pdf_content_stream` per
stream, per document, and over 120 randomized adversarial streams. The port's
real hazard, caught by deriving tables from CPython rather than assuming:
**65 byte values above 0x7F satisfy Python's `chr(b).isalpha()`** — an
`is_ascii_alphabetic()` port would silently drop text (same class as 34.5's
`str.lstrip("./")` catch); cp1252's five undefined bytes are transcribed too.

**Two pre-existing bugs found and fixed** (both surfaced by touching the
reference config, both verified before fixing, both regression-tested):
(1) **`pheasant.example.yaml` had two top-level `sync:` keys** — YAML keeps the
last, so the whole documented `sync.limits` guardrail block (the "I accidentally
indexed my home directory" protection) was **silently discarded**; proven with
real PyYAML, then merged. (2) **`yaml.py` never stripped trailing comments** —
it skips whole-line ones only, so in the dependency-light environment
`max_files: 50000  # ...` parsed as a *string* and `follow_symlinks: false # ...`
became a **truthy** string, inverting a safety default; fixed with quote-aware
stripping matching PyYAML. Note this shim shadows real PyYAML from the repo
root, which is also why `mkdocs` must build with `-f` from outside the checkout.

`pheasant.example.yaml` gained its **first `ingestion:` block** (captioner/
transcriber were never in it either) and `docs/configuration.md` its first
`## ingestion` section, closing pre-existing 25.4 doc drift. New
`docs/how-to/document-ingest.md` (in the nav). Acceptance:
`tests/test_document_extraction.py` (30) + 4 config tests —
**mutation-tested, not trusted**: disabling `extractor_from_config` fails 5
tests across every acceptance path, and search precision was checked directly
(each marker query returns exactly its one document; a nonsense query returns
`[]`) so the assertions aren't vacuous. Suite: **592 passed / 18 skipped**
(baseline 521/24; the skip drop is `wasmtime` un-skipping 6 module-level
guards — 34 genuinely new tests). Full detail:
`runs/2026-08-06-pdf-extraction/SUMMARY.md`.

### Five more formats, 2026-08-06 — `.pptx/.xlsx/.doc/.rtf/.epub`

The first pass ruled these out as "in no extension set, so not a broken
promise". Asked to include them, they now extract too. `DOCUMENT_EXTENSIONS`
grows to **seven** formats, and `EXTRACTED_EXTENSIONS` in `extractor.py` is
asserted **set-equal** to it — the accept-list and the extractor-build gate
drifting apart is precisely how the original bug would come back one format at
a time (a source carrying only `.pptx` would accept the files and build no
extractor → zero chunks). `extract_builtin(kind, content)` is now the single
format→reader map that all four providers share.

New `ingestion/office.py` (PPTX/XLSX/EPUB/RTF) + `ingestion/msdoc.py` (legacy
binary DOC), both **pure stdlib**. Notable behaviors, each chosen over an
easier wrong one: PPTX walks `<a:p>` paragraphs (not a flat `<a:t>` sweep, which
runs bullets together) and **indexes speaker notes**, located via each slide's
own `_rels` rather than assuming `notesSlideN`↔`slideN`; XLSX resolves
`sharedStrings` and maps sheet names through `workbook.xml.rels`, emitting
tab-separated rows; EPUB reads in **OPF spine order** (filename order scrambles
most real books — guard test with deliberately anti-alphabetical names); RTF
skips ignorable destinations (`\fonttbl`/`\colortbl`/`\stylesheet`/`\info`,
whose `\'hh` escapes would otherwise decode into mojibake) and honours
`\ucN` so `\uN` fallback characters don't double every non-ASCII char. Every
element match is on **local name**, not a hard-coded namespace URI. `native`
now reads EPUB via pymupdf (a real upgrade — MuPDF walks reading order); for
PPTX/XLSX/RTF/DOC `native` is honestly the *same code path* as `builtin`, since
no third-party reader for them exists in the dep tree and the builtin readers
are complete.

**`.doc` is the one that needed real work.** It is an OLE2 Compound File
(a small FAT filesystem) whose text is not contiguous: the `WordDocument`
stream holds character data and a **piece table** in `0Table`/`1Table` says
which byte ranges form the document and whether each piece is single-byte
cp1252 or UTF-16. So `msdoc.py` implements a CFB reader (header/DIFAT/FAT/
miniFAT/directory) plus a FIB walk — `fcClx` is pair index 33 of `FibRgFcLcb`,
reached by *walking* `csw`/`cslw`/`cbRgFcLcb` since the FIB is variable-length,
not by a hard-coded offset. Pre-Word-97 layouts are **refused** (`cbRgFcLcb <
34`) rather than read at index 33 anyway, which would yield confident garbage.
Field ranges are handled: text between `0x13`/`0x14` is the field *instruction*
(`HYPERLINK "http://…"`) and is dropped, `0x14`/`0x15` is the *result* and is
kept — skipping that distinction is why naive extractors emit link machinery
mid-sentence. Bounded throughout (visited-set chain walks so a cyclic FAT
terminates, caps on stream/piece/text size); Python's bounds-checked slicing
means hostile offsets raise rather than corrupt, which is also why `.doc`
doesn't get a WASM guest — it's the most attacker-facing format here, but the
failure mode is already an exception, not memory corruption.

**LibreOffice Writer/Calc/Impress were installed to generate the fixtures**, so
all five are **producer-generated, not hand-authored** — decisive for `.doc`,
where a hand-crafted CFB would only prove the reader agrees with its own
author's reading of the spec. The container layer is additionally cross-checked
byte-for-byte against **olefile** (an independent implementation, deliberately
*not* a dependency — `importorskip`). Two branches the fixtures can't reach are
unit-tested with the reason recorded: **compressed cp1252 pieces** (LibreOffice
always writes UTF-16; real Word prefers compressed, and that path has the
unusual `fc // 2`) and field-instruction ranges.

**Two real bugs caught during development, both by markers deliberately planted
in the fixtures.** (1) The `.doc` control-code drop set included `0x28/0x3C/
0x3E` on the mistaken belief they were Word markers — they are printable ASCII
`(`, `<`, `>`, so **every** extracted document silently lost its parentheses
and angle brackets. Now `_DROP` carries an assertion that every entry is below
`0x20`, plus a regression test. (2) A non-RTF file named `.rtf` decoded its raw
bytes as latin-1 prose; RTF has no structural gate of its own, so the `{\rtf`
signature is now required — mojibake in the index is worse than no text,
because it is unfindable *and* pollutes scoring.

Acceptance: `tests/test_office_extraction.py` (54). Mutation-tested: narrowing
`source_includes_documents` back to PDF+DOCX fails **10** tests; stubbing
`extract_builtin`'s pptx/doc arms fails **6**. Search precision checked
directly (a nonsense query returns `[]`). Suite: **646 passed / 18 skipped**
(+54). Docs: `configuration.md` §ingestion.extractor (seven-format table),
`how-to/document-ingest.md` (retitled, per-format gotchas). Router side needs
nothing new — `"document"` already covers all seven.

---

## Structural taxonomy extraction, 2026-08-06 — chapters, sections, § codes

Asked for, for "documents and books and other highly structured documentation
like procedures, and legal documents": extract a taxonomy by chapter / section /
code, **toggleable on source registration**. It turned out to be three dormant
pieces that were already specified, not a new subsystem:

1. **`TextChunk.heading_path` was never populated.** It is a field, a `chunks`
   column, *and* a `chunks_fts` column at **BM25 weight 2.0 — double the body
   text**. `chunks_for_source` simply never passed one, so it was `NULL` for
   every chunk ever indexed (verified: 0 of 3 on a real sync).
2. **`heading` node type and `has_heading` edge type are documented in
   `docs/graph_model.md`** (lines 13/24) and `grep` found **zero** emissions
   anywhere in the code.
3. **The one heading detector that existed was dead code.** `enrichment.py:218`
   regexes Markdown headings into `_add_concept`, which returns early since the
   2026-08-03 concept retirement — it runs the regex and discards the result.

New `src/pheasant/ingestion/taxonomy.py`: rule-based, deterministic, offline
(no model, no network — rule 1 by construction). Six rules, each with its own
natural depth so a document may **mix conventions** and still nest —
`ARTICLE IV` → `4.1` → `(a)` works, as does `# Title` → `1. Scope`: `markdown`
(1-6), `keyword` (PART/TITLE/BOOK/DIVISION/SCHEDULE/APPENDIX/ANNEX/EXHIBIT=1,
CHAPTER/SUBPART=2, ARTICLE/SECTION/RULE=3, CLAUSE/STEP/PARAGRAPH=4), `code`
(`§ 12.3`), `numbered` (`1.2.3`), `lettered` (`(a)`/`(iv)`), `caps`. Nesting is
a stack walk; `number` is kept separate from `title` so a section is findable by
its **citation**. A **bare citation takes its caption from the next line**
(`ARTICLE IV\nTerm and Termination` → `Article IV Term and Termination`) —
without it, standard legal drafting loses the words a searcher would actually
use.

**Chunks are cut at section boundaries** (`split_on_sections`, on by default
once taxonomy is on). This was a deliberate change of mind mid-build: labelling
alone is nearly decorative, because a whole contract fits in one 4000-char chunk
and gets one `heading_path` (its first heading), and a chunk straddling three
sections is labelled with only the first — *misleading*, not merely coarse. Now
one chunk is one section, subdivided only when a section exceeds
`chunking.max_chars` (all pieces sharing the path), with absolute line numbers
and a single ascending index run so chunk IDs stay stable.

**Heading node IDs key on the section breadcrumb, hashed — deliberately not on
the line number.** A line-numbered ID churns the whole graph on any edit:
inserting one paragraph shifts every heading below it, dropping and re-creating
sections that did not change. Asserted by a test that inserts a line and
compares ID sets. Accepted trade-off: two byte-identical breadcrumbs in one
document collapse to one node.

**Search now says which section matched.** `heading_path` was already selected
by the search SQL and dropped when the result dict was built; it is now on the
result, its `chunks[]` entry and `provenance` — added **only when non-empty**, so
a corpus without taxonomy returns the exact payload it did before.

**Toggle lives at source registration**, matching the request: per-source
`sources[].taxonomy.{enabled,max_depth,detect,graph_nodes,split_on_sections}`
(default **off**), threaded through `POST /sources` + `PATCH` (dict, exactly like
`chunking`/`sync`/`connector`), `POST /sources/quick-add` (plain bool, it is the
one-field surface) and MCP `register_source(taxonomy=True)` (additive optional
param — rule 8). New `GET /taxonomy?source=&path=` renders the outline per
document as a nested tree, read from the emitted `heading` nodes rather than
re-parsing.

**Off by default and per-source for a stated reason**, not caution: the
numbering rules are genuinely ambiguous on prose — `1. Introduction` in a
standard is a section, `1. Buy milk` in a note is a list item, and nothing in
the line distinguishes them. Length/punctuation filters (≤120 chars, ≤10 words
chosen against real headings, no mid-sentence punctuation) catch the long cases;
short list items still match. Enabling per source is how the operator says "this
corpus really is structured". Two further limits documented rather than hidden:
a document mixing two **independent** numbering series can mis-parent one under
the other (`§ 12.3` under `ARTICLE IV`) — the breadcrumb still carries the right
citation so search is unaffected, only the parent link is wrong; and `caps` is
the noisiest rule, first to drop from `detect`.

`docs/graph_model.md` now records that `heading`/`has_heading` went **unemitted
from the initial build until 2026-08-06**, so a graph written earlier contains
none — that matters for anyone reading the taxonomy as a contract (rule 3).
Docs: `configuration.md` §`sources[].taxonomy`, new
`docs/how-to/structured-documents.md` (in the nav), first per-source `taxonomy:`
block in `pheasant.example.yaml`. Acceptance: `tests/test_taxonomy.py` (40) —
**mutation-tested**: disabling detection fails 12, dropping the graph nodes 5,
re-dropping `heading_path` from search results 1, turning off section splitting
4. Suite: **686 passed / 18 skipped** (+40). No wire-format change (the Synapse
contract is untouched; parity green), no new dependency.

---

## 6. Pointers

- **Region-side Synapse spec:** `docs/SYNAPSE_INTEGRATION.md`
- **System architecture + framework (other repo):**
  `pheasant-flock/docs/SYNAPSE_ARCHITECTURE.md`,
  `…/docs/SYNAPSE_FRAMEWORK.md`, ADR 2026-06-10 in `…/docs/DECISIONS.md`
- **Graph taxonomy:** `docs/graph_model.md` · **Config:** `docs/configuration.md`
- **MCP:** `docs/mcp_tools.md`, `docs/mcp_client.md`
- **Deployment:** `docs/deployment.md`, `deploy/kubernetes/`

If docs drift from code, **the code is authoritative** — flag the drift in
`docs/SYNAPSE_INTEGRATION.md` (Synapse scope) or the relevant doc.
