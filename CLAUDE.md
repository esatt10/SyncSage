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

syncsage init --profile quickstart         # generate starter syncsage.yaml
syncsage validate && syncsage doctor       # config + environment checks
syncsage start                             # HTTP API + MCP on :8765
syncsage sync --source <name> --mode incremental|full|validate_only|repair
syncsage mcp --transport stdio             # standalone MCP server
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

**Heterogeneous embedding spaces (2026-07-11) [x-repo] — docs-only here.**
The Synapse router (subjective-retrieval, ADR 2026-07-11) now routes
**mixed-embedding-space fleets** by partitioning contracts per
`embedding_space (model_id, dim, normalized)` and scoring each partition with
a query vector in that space (`synapse.spaces` per-space query embedders /
`query_vecs`); unresolvable spaces are excluded per query with
`embedding_space_unresolved`. **No SyncSage code change** — a region already
embeds with its own `search.embeddings` model and publishes its own
`embedding_space`; wire format unchanged (no schema bump, no re-vendor,
parity green). See `docs/SYNAPSE_INTEGRATION.md` §3 note. Suite: **152
passed / 2 skipped** (unchanged).

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
