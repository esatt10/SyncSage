# Synapse Integration — SyncSage as a Brain Region

**Status:** authoritative SyncSage-side spec (2026-06-10). The system-wide
design lives in the **subjective-retrieval** repository:
`docs/SYNAPSE_ARCHITECTURE.md` (architecture) and
`docs/SYNAPSE_FRAMEWORK.md` (execution plan, phases 20–26). This document
mirrors **Phase 21 — region hardening**, which executes *here*, plus the
cross-repo contract obligations.

---

## 1. SyncSage's role in Synapse

Synapse is a hyperfast federated knowledge-base system. In its brain
metaphor:

- **SyncSage instances are the regions**: each container owns one
  specialized knowledge base — sync engine, SQLite/FTS5 state, knowledge
  graph, self-search, MCP/HTTP surfaces. Sizes range from single-digit MB
  to multi-TB. Regions are fully self-contained and deploy standalone.
- **Subjective-retrieval is the nervous system**: a router that decides
  *which regions to ask* by scoring each region's published **semantic
  contract**, fans the query out to the chosen regions' self-search, and
  merges/re-ranks the answers.
- **The semantic contract** is the artifact a region derives from its own
  content and publishes after every successful sync: embedding-space
  signature (centroid, covariance diagonal, ≤32 cluster centroids),
  concept vocabulary (from this repo's graph concepts) + MinHash,
  capability descriptor, freshness watermark. Bounded ≤ 256 KB.

Two integration invariants:

1. **The contract schema is owned by subjective-retrieval.** Its Pydantic
   model is canonical; this repo vendors only the exported JSON Schema
   under `contracts/` plus golden fixtures, with a CI parity test
   (sha256 equality with the other repo's fixtures). Never hand-edit the
   vendored files.
2. **No Python dependency between the repos.** The boundary is the
   contract JSON + HTTP. A region must never import subjective-retrieval;
   the router never imports syncsage. Regions must keep working with no
   router configured (all Synapse behavior is a no-op when
   `synapse.router_url` is unset).

---

## 2. Phase 21 — region hardening (executes in this repo)

These steps fix the gaps found in the 2026-06-10 audit and make a SyncSage
container a production-grade region. One step per agent session; each run
writes `runs/<ts>-synapse-<step>/SUMMARY.md` (create `runs/` +
gitignore it). Steps marked **[x-repo]** require matched work in
subjective-retrieval via its `syncsage-coordinator` skill — identical
branch name in both repos, both test suites green before either push.

### Step 21.1 — Real watcher + scheduler

**Gap:** `src/syncsage/sync/watcher.py` and `sync/scheduler.py` are stubs;
no live incremental sync despite `sync.watcher.*` / `sync.scheduler.*`
config keys existing in `config/schema.py`.

**Does:** implement `WatcherService` on `watchdog` observers per enabled
filesystem source: collect events, debounce (`sync.watcher.debounce_ms`),
batch, then call `SyncEngine.sync_source(name, mode="incremental")`.
Implement `SchedulerService` as a daemon-thread interval loop
(`sync.scheduler.interval_seconds`) calling `sync_all("incremental")` as
the fallback for non-filesystem sources. Both started/stopped by
`syncsage start`; both no-ops when disabled in config.

**Acceptance:** integration test: touch a file in a watched fixture
source → only that artifact re-indexes within debounce + 2 s (assert via
sync_events + manifest); scheduler fires ≥ 3 times at a 1 s test interval;
clean shutdown leaves no threads.

**Files:** `sync/watcher.py`, `sync/scheduler.py`, `cli.py` (start
wiring), `tests/test_watcher.py`, `tests/test_scheduler.py`.

### Step 21.2 — Crash-safe state: WAL + writer lease + manifests→SQLite

**Gap:** SQLite opened without WAL; graph JSON + manifests can tear under
crash or concurrent engines; manifests are loose JSON
(`persistence/manifest.py`) beside the authoritative DB — split-brain
risk.

**Does:** (a) `StateStore` opens with `journal_mode=WAL`,
`busy_timeout=5000`, `synchronous=NORMAL`. (b) Single-writer **lease
file** `<state>/engine.lease` (PID + heartbeat timestamp, refreshed every
5 s); a second engine process exits with a clear error; stale lease
(no heartbeat > 30 s) is taken over. (c) Manifests move into a
`manifests` table (one row per source, JSON column is fine); one-shot
idempotent startup migration reads each legacy
`/state/manifests/<src>.json`, inserts, renames to `*.migrated` (never
deletes). (d) Graph save already uses tmp+rename — add fsync.

**Acceptance:** `kill -9` mid-sync then restart: engine starts, state
consistent, incremental resync completes with no `repair` needed; second
concurrent engine exits non-zero with lease message; legacy manifest dir
migrates exactly once (second startup is a no-op).

**Files:** `persistence/state_store.py`, `persistence/manifest.py`,
`sync/engine.py`, `sync/locks.py`, `tests/test_crash_safety.py`.

**Implementation note (2026-06-10, landed):** the lease is acquired on the
first *sync* rather than at engine construction — acquiring at construction
would break the documented docker-exec MCP-stdio workflow (a second,
read-mostly engine process against the state dir of a running server). Any
writer (one-shot `syncsage sync`, startup sync, watcher, scheduler, HTTP
`/sync`) acquires the lease and holds it (heartbeating) until `close()`;
all 21.2 acceptance criteria hold unchanged. In-process writers are
additionally serialized by an engine-internal lock, closing the 21.1
startup-executor gap.

### Step 21.3 — True incremental web / API / S3 connectors

**Gap:** `sync/connectors.py` creates checkpoints but never consults
them; non-filesystem sources re-fetch everything every sync.

**Does:** `WebCollectionConnector` stores per-URL ETag/Last-Modified in
its checkpoint and sends conditional requests (304 → skip);
`APIConnector` consults `cursor_json`; `S3Connector` lists with the
checkpoint high-watermark (LastModified) and only reads new/changed keys.
Record skipped-vs-fetched counts in sync_events details.

**Acceptance:** mocked-transport tests: second sync of an unchanged web
collection performs zero body downloads (304s only); S3 second sync lists
but reads 0 objects; sync_events rows show `{"fetched": 0, "skipped": N}`.

**Files:** `sync/connectors.py`, `tests/test_sync_connectors.py`.

**Implementation note (2026-06-10, landed):** the previous checkpoint is
threaded via a new `SourceConnector.begin_sync(mode)` hook the engine calls
before `list_items()`; connectors consult it only in `incremental` mode
(`full`/`repair` always re-fetch), and pre-21.3 checkpoints degrade
gracefully to "no validator cached". Web 304s surface as an
`ItemNotModified` exception from `read_item` which the engine counts as a
skip. The API connector has no pagination in its current design, so
"consults `cursor_json`" is implemented as the minimal honest version that
design supports: a per-item `{identity: {sha256, mtime}}` map in the cursor
(unchanged `updated_at`/`mtime` reuses the cached content hash and skips the
item fetch) plus free hashing of listing-inline content. S3 caches
`{etag, size_bytes, mtime, sha256}` per object and skips `get_object` for
objects at-or-before the high-watermark with unchanged ETag/size.
`sync_events` counters: `fetched` = bodies actually transferred, `skipped` =
items skipped without a transfer — a no-validator web server therefore
counts re-downloads as fetched even when the post-fetch hash comparison
skips re-indexing (`indexed_artifacts`/`skipped_artifacts` are also
recorded).

### Step 21.4 — Per-region vector index + embedding provider **[x-repo]**

**Gap:** `search.embeddings` / `search.vector_store` config exists with
zero code paths; self-search is FTS + graph only.

**Does:** new `src/syncsage/search/vector_store.py`: `VectorStore`
protocol with a `lancedb` default backend (optional extra
`[vector]`) and a deterministic **stub embedder** for offline tests.
Embeddings are computed at sync time per chunk through an OpenAI-spec
HTTP embedding endpoint (`search.embeddings.base_url` / `model` /
`api_key_env`) — the same provider surface subjective-retrieval uses, so
a fleet pins one model for both. `HybridSearch` gains
`mode="vector"` candidates merged into `hybrid`. Capabilities reported by
the contract publisher gain `returns_vectors: true`,
`search_modes: [..., "vector"]`.

**Acceptance:** fixture test (stub embedder): a query phrased with
synonyms surfaces a lexically-absent chunk in `hybrid` mode that pure
`text` mode misses; sync remains idempotent (unchanged chunk → no
re-embed, keyed on `text_hash`); test suite stays fully offline.

**Files:** `search/vector_store.py` (new), `search/hybrid.py`,
`sync/engine.py` (embed-on-sync), `config/schema.py` (activate keys),
`pyproject.toml` (`[vector]` extra), `tests/test_vector_search.py`.

**Implementation note (2026-06-13, landed):** two `VectorStore` backends —
`NumpyVectorStore` (always available; flat fsync'd `index.json` of
base64-float32 vectors under `<state>/vectors/<kb_id>/`; the offline test
backend) and `LanceDBVectorStore` (production default, lazy import with a
`pip install 'syncsage[vector]'` hint). Idempotency bookkeeping is
content-addressed: chunk ids embed the chunk `text_hash`, so "already
embedded?" is exactly store membership — `VectorIndexer` embeds only
missing ids and prunes ids absent from the `chunks` table at sync end
(re-syncing unchanged content performs zero embedder calls, asserted in
`tests/test_sync_idempotency.py`). `StubEmbedder` hashes tokens (blake2b)
to fixed unit directions with a small synonym canonicalization table so
hybrid-vs-text acceptance runs offline. The **[x-repo]** obligation was
satisfied by wire conformance, not code: `OpenAISpecEmbedder` speaks the
standard OpenAI embeddings HTTP shape the subjective-retrieval provider
layer uses, so a fleet pins one model for both repos; no sibling-repo
change was needed. `numpy` was promoted to a core dependency (the numpy
backend + stub must work without `[vector]`).

### Step 21.5 — Contract publisher + sync event stream **[x-repo]**

**Gap:** no sync-completion signal (`sync_events` is write-only); no
contract; the router has nothing to route on.

**Does:** new `src/syncsage/synapse/` package:
- `publisher.py`: on every `sync.completed`, derive the semantic contract —
  signature from 21.4 chunk vectors (streaming mean, covariance diagonal,
  seeded mini-batch k-means m ≤ 32), vocabulary from graph concept nodes
  (top ≤ 256 by weight) + 128-perm MinHash, watermark =
  sha256 over ordered source checkpoint digests, capabilities from config.
  Validate against the vendored JSON Schema, write
  `/state/contract.latest.json` (tmp+rename), serve via
  `GET /contract` (FastAPI) and MCP resource
  `syncsage://knowledge-bases/{kb_id}/contract`.
- `events.py`: append-only NDJSON event log `/state/events/YYYY-MM-DD.ndjson`
  (`sync.started|completed|failed`, `source.changed`); when
  `synapse.router_url` is set, POST `sync.completed` to
  `<router>/v1/synapse/events` (timeout 5 s, failures logged not raised).

**Acceptance:** contract validates against `contracts/
semantic_contract.v1.schema.json`; two syncs with unchanged content yield
identical watermark digests; webhook fires exactly once per completed
sync against a test server; everything is a no-op with `synapse.*` unset
except the local contract file + `GET /contract`.

**Files:** `synapse/publisher.py`, `synapse/events.py`, `api/app.py`
(`GET /contract`), `mcp_server/{tools,resources}.py`,
`config/schema.py` (`synapse.router_url`, `synapse.fleet_id`,
`synapse.publish: bool`), `tests/test_contract_publisher.py`,
`tests/test_contract_parity.py`.

**Implementation note (2026-06-14, landed):** new `src/syncsage/synapse/`
package (region-side; not the sibling's `synapse/`). The contract is built **by
hand** to the vendored `contracts/semantic_contract.v1.schema.json` — no import
of subjective-retrieval. Verified byte-for-byte against the sibling's canonical
model: the fp16-base64 vector codec, the u64-base64 + blake2b MinHash, the
`sha256:`-prefixed canonical-JSON `integrity.content_hash`
(`sort_keys`/compact-separators/`integrity`-excluded), and the file
serialization (`indent=2, sort_keys, ensure_ascii=False` + trailing newline)
all produce output that feeds back into the sibling's `SemanticContract.seal()`
/ `to_json_text()` identically and passes `verify_content_hash()`.

- **MinHash scheme: MATCHES the sibling exactly.** `_minhash_signature`
  reimplements the documented 128-perm scheme — permutation `i` hashes term `t`
  as the low 64 bits (big-endian) of `blake2b(f"{i}:{t}", digest_size=8)`, min
  across the term set, encoded as base64 of a uint64 array. A standalone parity
  check confirms `numpy.array_equal` with the sibling's `minhash_signature` for
  populated and empty term sets, so cross-repo white-matter overlap (Step 23.1)
  can compare region MinHashes directly.
- **Signature:** centroid = mean, `covariance_diag` = population variance, and
  ≤32 cluster centroids from a deterministic seeded Lloyd's k-means (pure numpy,
  no sklearn — works without the `[vector]` extra), `m = min(32, ⌊√n⌋, n)`,
  strided ≤16,384-row sample, clusters canonically ordered by
  `(-weight, fp16_payload)` for byte-stable output. Vectors are read from the
  21.4 vector store via an additive `all_vectors()` bulk reader added to both
  `NumpyVectorStore` and `LanceDBVectorStore`.
- **No-vector fallback (decision: option b — degenerate signature, NOT a
  no-op):** when `search.embeddings.enabled` is false there are no chunk
  vectors, so the publisher emits a schema-valid contract with a *degenerate
  zero signature* (zero centroid/covariance, no clusters, `sample_count=0`) and
  `embedding_space` from the configured embed model/dim, while still carrying
  the real vocabulary + watermark. Capabilities truthfully drop `"vector"` from
  `search_modes` and set `returns_vectors=false`. This keeps the region
  routable on vocabulary/freshness even without embeddings.
- **Watermark digest:** `source_checkpoints_digest` = sha256 over each source's
  ordered `(source_id, relative_path, sha256)` content fingerprint from the
  artifacts table — deliberately **not** the connector checkpoints, whose
  `high_watermark` carries transient per-sync `indexed`/`skipped` counters that
  would make two no-change syncs disagree. Two syncs with unchanged content
  produce the identical digest (acceptance-gated, byte-identical with a pinned
  `generated_at`).
- **Publish default + gating:** `synapse.publish` defaults **off**. The entire
  21.5 hook (`contract.latest.json` write + NDJSON `sync.completed` event +
  router webhook) is gated by it, so a router-less standalone region is
  byte-for-byte unchanged. `GET /contract` (FastAPI) and the MCP resource
  `syncsage://knowledge-bases/{kb_id}/contract` (+ `get_contract` tool) serve
  the on-disk contract (404 / `{"status":"unpublished"}` before first publish).
- **Webhook:** when `synapse.router_url` is set, the engine POSTs the
  `sync.completed` event with the inline `contract` to
  `<router>/v1/synapse/events` (the sibling's 20.3 endpoint shape) via stdlib
  `urllib` (no new dependency), 5 s timeout; failures are logged, never raised,
  so a router-down fleet still syncs.

### Step 21.6 — Snapshots, compression, retention, backup + cross-source edges

**Gap:** `graph.latest.json` is overwritten with no history; `storage.
compression` / `graph_snapshot_interval_seconds` / `max_state_size_gb`
config keys are dead; no backup/restore; graph has no cross-source edges
and concepts are un-normalized.

**Does:** *(session A — persistence)* zstd-compressed timestamped
snapshots `graphs/<kb>/graph.<ts>.json.zst` on the configured interval;
retention deletes oldest snapshots beyond `max_state_size_gb`;
`syncsage backup <out.tar.zst>` / `syncsage restore <in>` covering
SQLite (via `VACUUM INTO`), graphs, contract, events.
*(session B — graph)* cross-source edge pass: python imports / markdown
links whose targets resolve into a *different* source produce
`references`/`imports` edges across sources; concept normalization
(lemma-light: lowercase, singularize, dedupe) before node creation.

**Acceptance:** (A) restore on a fresh state dir reproduces identical
node/edge counts and passing FTS queries; snapshot retention respects the
size cap in a small-cap test. (B) fixture workspace (repo + notes
sources) yields a cross-source `references` edge; concept count drops vs.
un-normalized baseline with no search-quality regression in existing
tests.

**Files:** `persistence/graph_store.py`, `persistence/state_store.py`,
`cli.py` (backup/restore), `graph/{builder,enrichment}.py`,
`pyproject.toml` (`zstandard`), tests.

**Implementation note (2026-06-18, session A landed; session B pending):**
this session implemented **only the persistence half** (snapshots + retention +
backup/restore). The graph half (cross-source edges + concept normalization) is
untouched and remains for **session B** — `graph/{builder,enrichment}.py` were
not modified.

- **Snapshot layout.** After a successful sync, `GraphStore.write_snapshot`
  writes `graphs/<kb_id>/graph.<utc-ts>.json.zst` — the same node-link JSON
  payload as `graph.latest.json` (`indent=2, sort_keys=True`), zstd-compressed
  (level 10). The `<utc-ts>` is the ISO-8601 `utc_now()` with `:` → `-` so the
  filename stays filesystem-safe **and** lexically sortable (chronological).
  `graph.latest.json` stays **uncompressed** for fast hot-path load; snapshots
  are additive history. Both snapshot and latest writes are durable
  (tmp + fsync + `os.replace` + best-effort dir fsync), reusing the 21.2
  pattern.
- **Interval throttling.** `SyncEngine._snapshot_after_sync` writes at most one
  snapshot per `storage.graph_snapshot_interval_seconds`: it parses the newest
  existing snapshot's embedded timestamp and skips writing when the elapsed time
  is below the interval (two syncs within the window → one snapshot; a sync after
  it → a second). Snapshotting + retention are fail-soft (a hiccup is logged,
  never fails the sync).
- **Retention policy.** `GraphStore.enforce_retention(kb_id, max_bytes)` deletes
  **oldest snapshots first** until the total `.zst` byte size for the KB is at or
  under `max_state_size_gb` (× 1024³). It **never** deletes `graph.latest.json`,
  the SQLite db, or `contract.latest.json`, and always keeps at least the newest
  snapshot even if it alone exceeds the cap. `max_state_size_gb` is now typed
  `float` so small-cap tests can use sub-GB values.
- **Backup format.** `syncsage backup <out.tar.zst>` (new
  `persistence/backup.py`) writes a zstd-compressed tar of the durable state:
  `syncsage.db` taken via **`VACUUM INTO` a temp file** (a consistent snapshot
  under WAL — never a raw copy of the live db; manifests live in the db since
  21.2 so they ride along), plus `graphs/`, `events/`, `vectors/`, any legacy
  `manifests/` dir, and `contract.latest.json` when present. The live state dir
  is read-only during backup.
- **Restore safety.** `syncsage restore <in.tar.zst>` decompresses + extracts
  into a sibling temp dir (with path-traversal/absolute-member guards), runs
  `PRAGMA integrity_check` on the restored db, and only then swaps it in. It
  **refuses a non-empty target** (non-zero exit, clear message) unless `--force`;
  on `--force` the existing dir is renamed aside to `<name>.replaced-<ts>`
  (preserved, never deleted) before the staged tree is moved into place — so a
  failure never partially destroys the user's state.
- **Config defaults.** `storage.graph_snapshots` (new, default **on**),
  `storage.compression` (new, `"zstd"`), and the previously-dead
  `graph_snapshot_interval_seconds` (900 s) / `max_state_size_gb` (10) are now
  live. Standalone behavior stays sane: snapshots are additive, bounded, and
  fully local — no Synapse/router involvement.
- **New dependency.** `zstandard>=0.22` promoted to a **core** dependency
  (snapshots/backup must work without any extra). It is the only new dep for
  this step.

**Implementation note (2026-06-18, session B landed — closes Step 21.6 and
Phase 21):** this session implemented **only the graph half** (cross-source
edges + concept normalization). The persistence half (session A) was untouched:
no snapshot/backup/retention code (`persistence/graph_store.py` snapshot logic,
`persistence/backup.py`, `backup`/`restore` CLI) was modified. Touched:
`graph/enrichment.py`, `graph/builder.py`, `sync/engine.py`, new
`tests/test_cross_source_edges.py`. No new dependencies; no stable-ID grammar
change (rule 3) — only new edges between existing IDs and concept surface-term
normalization that keeps id derivation deterministic.

- **Cross-source resolution timing.** A new global post-pass,
  `GraphBuilder.add_cross_source_edges()`, runs in `SyncEngine.sync_source`
  *after* the per-source `add_similarity_edges` and *before* the graph save (it
  mirrors the post-hoc `SemanticSimilarityPass`). It must run after each
  source's enrichment is applied because references can only resolve once both
  the referencing and the target source are present in the accumulated graph
  (sources sync independently). The pass walks the whole graph each time;
  edges are **upserted** so a re-sync produces an identical graph (idempotent).
- **Resolution rules (deterministic, rule-based, no LLM).** The pass inspects
  every `artifact → external_reference` edge of type `imports` or `references`:
  - `reference_type == "python_import"`: the dotted module is mapped to
    candidate relative paths `pkg/mod.py` then `pkg/mod/__init__.py`; the first
    that matches an artifact (exact relative-path, else `/`-suffix match)
    yields an **`imports`** edge.
  - `reference_type in {"document_link","url"}`: the link target (fragment/query
    stripped, leading `./` trimmed, `http(s)`/`mailto` skipped) is matched by
    exact relative path, then — for extension-less wiki links — `.md`/`.txt`,
    then `/`-suffix match; a hit yields a **`references`** edge.
  Only matches whose target lives in a **different** source produce an edge
  (intra-source resolution is already covered by existing enrichment and is
  intentionally skipped here). Cross-source edges carry
  `{cross_source: true, target_source_id, reference, reference_type,
  enrichment_pass: "cross_source_resolution"}`. Unresolvable references keep the
  existing `external_reference` node + edge unchanged (no regression). Output is
  sorted `(source, target, type)` for byte-stable snapshots.
- **Concept normalization rule set.** Before a `concept` node is created,
  `_normalize_concept` applies the existing `_normalize_term` (lowercase + slug)
  and then a lemma-light per-token singularizer `_singularize_word` (pure
  python, no NLP dep): for tokens length ≥ 4 not in a `SINGULARIZE_STOPLIST`
  and not ending in `ss` — `…ies → …y` (libraries → library),
  `…(s|x|z|ch|sh)es → drop es` (boxes → box), other `…s → drop s`
  (systems → system). The transform is idempotent so an already-singular term
  is a no-op, keeping the concept's stable id derivation consistent across
  syncs. Candidates are singularized up front in `_concept_candidates` so
  plural/singular surface forms count toward one frequency bucket and collapse
  to a single node. `STOPWORDS`/`SINGULARIZE_STOPLIST` guard non-plural words
  (status, analysis, css, …). Concept ids stay `concept:<kb>:<source>:<term>`;
  only the `<term>` is now the normalized/singular surface (rule 3 honored).
- **Fixtures.** Cross-source acceptance uses self-contained workspaces built in
  `tests/test_cross_source_edges.py` (a code source importing a module + linking
  a doc that live in a second source); no shared-fixture edit was needed.
- **Standalone unchanged.** The pass is fully local, additive, and
  router-independent; idempotency (`tests/test_sync_idempotency.py`) stays green.

**Phase 21 exit criterion:** a single SyncSage container survives
kill-mid-sync, live-watches its sources, answers vector+hybrid
self-search offline-testably, publishes a schema-valid contract on every
sync, can be backed up and restored byte-faithfully — with the standalone
(non-fleet) mode untouched.

---

## 3. Contract obligations (quick reference)

| Obligation | Where |
|---|---|
| Vendored JSON Schema | `contracts/semantic_contract.v<N>.schema.json` (do not edit; re-vendor from subjective-retrieval) |
| Golden fixtures | `contracts/fixtures/*.json` — byte-identical with the other repo (`tests/test_contract_parity.py`) |
| Publisher | `src/syncsage/synapse/publisher.py` (Step 21.5) |
| Serving | `GET /contract` + MCP resource |
| Push | `POST <router>/v1/synapse/events` on `sync.completed` |
| Embedding-space pin | `search.embeddings.model` must equal the fleet pin or the router rejects the contract (HTTP 409) |
| Signing (optional, Step 24.4) | `synapse.signing_key_ref` → `src/syncsage/synapse/signing.py` Ed25519-signs `integrity.signature`; router rejects (HTTP 403) under `require_signed` |

### Step 24.4 — Ed25519-signed contracts + A2A (2026-06-20, [x-repo])

A region can **optionally sign** its semantic contract so the router can verify
authenticity/integrity beyond the `content_hash`:

- **Config:** set `synapse.signing_key_ref` to a secret *reference*
  (`env://NAME` or a bare env-var name). The referenced value is the base64 of a
  32-byte raw Ed25519 private seed — the plaintext key never lands in YAML or on
  disk. **Unset (default) → unsigned** (`integrity.signature: null`); a
  **standalone, router-less SyncSage is entirely unchanged**.
- **What gets signed:** the *exact same canonical body bytes* the
  `integrity.content_hash` covers (body with `integrity` excluded,
  `sort_keys=True`, compact separators, `ensure_ascii=False`). The signature
  lives outside the hashed body, so signing never perturbs the content hash.
  `src/syncsage/synapse/signing.py` (`sign_body`/`signing_bytes`) is the
  region-side codec; it is byte-compatible with the router's
  `SemanticContract.verify_signature`, guarded by the cross-repo signing-parity
  fixture `contracts/fixtures/signed-demo-region.v1.contract.json` (+ PARITY).
- **Out-of-band public key (decision):** the router holds the kb_id→public-key
  trust store in *its own* config (`synapse.trust.keys`) and enforces
  `synapse.require_signed`. The public key is **not** added to the contract, so
  the **contract wire format / vendored JSON Schema are unchanged** — no
  schema-version bump, no re-vendor.
- **Optional dependency:** the `cryptography` import is gated behind the new
  `[a2a]` extra (`pip install 'syncsage[a2a]'`). A region without
  `signing_key_ref` needs no crypto dep; the offline suite passes without it.

## 4. Deployment notes

A region remains the existing container (`Dockerfile`, port 8765, PVC on
`/state`). In a Synapse fleet:

- **Compose** (Synapse Step 25.1, other repo): N syncsage services + 1
  router; each region gets `synapse.router_url` pointing at the router.
- **Kubernetes** (Step 25.2): one StatefulSet + PVC + headless Service
  per region, rendered from the router repo's Helm chart `regions:`
  values; this repo's `deploy/kubernetes/` manifests are the pod-spec
  baseline.
- Auth: regions accept a bearer token (`security` settings) minted by the
  router's tenancy layer; local/demo fleets may run open.
