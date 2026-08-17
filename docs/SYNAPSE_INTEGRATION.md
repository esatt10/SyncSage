# pheasant as a Synapse region

!!! abstract "For consumers — start here"
    pheasant can act as a **region** in a **Synapse** fleet: it publishes a
    bounded **semantic contract** describing what it knows, and a Synapse
    router uses that contract to route global, cross-region queries to it.

    - **Want to attach your KB to a fleet?** Read the task-focused guide:
      [Attach to a Synapse fleet](how-to/attach-to-synapse.md). That's all most
      consumers need.
    - **Standalone is the default.** Every Synapse setting is off unless you opt
      in via the `synapse:` config block; a router-less pheasant is unchanged.
    - **The global search experience** (routing, fan-out, merge, cross-region
      "white matter") lives on the router —
      [pheasant-flock](https://github.com/esatt10/pheasant-flock).

    Everything **below this box is the internal, contributor-facing spec**: the
    region-side contract obligations and the Phase-21 region-hardening step
    contracts. Consumers can safely stop here.

---


## Decision 2026-08-03 — contract vocabulary now comes from the FTS index

`vocabulary.top_concepts` and its MinHash used to be read from
`artifact_terms` rows of `node_type='concept'`. Concept extraction was retired
this day (see `docs/graph_model.md` and `graph.enrichment._add_concept`), so
that source no longer exists.

**The wire format is unchanged.** `top_concepts` keeps its shape and weight
scale, the MinHash is computed over the same kind of term set, and the router
scores contracts exactly as before — so the vendored schema and fixtures are
untouched, there is no schema bump, and `tests/test_contract_parity.py` stays
green without a re-vendor. This is a change of *source*, not of contract.

Terms now come from `chunks_vocab`, an `fts5vocab` view over the FTS index.
That is strictly better provenance for a region advertising what it knows: the
vocabulary is literally what is searchable in the region, it cannot drift from
the index, and it costs no storage — SQLite already maintains the term →
document-frequency table. Weights are document frequencies normalized to
(0, 1] against the most common term, so they stay comparable across regions of
different sizes. Ordering by *document* frequency rather than raw count is
deliberate: a term repeated 400 times in one file describes that file, while a
term appearing once in 400 files describes the corpus.

Rule 6 note: the contract schema remains canonical in pheasant-flock and
nothing under `contracts/` was hand-edited.

## Internal spec — pheasant as a Brain Region

**Status:** authoritative pheasant-side spec (2026-06-10). The system-wide
design lives in the **pheasant-flock** repository:
`docs/SYNAPSE_ARCHITECTURE.md` (architecture) and
`docs/SYNAPSE_FRAMEWORK.md` (execution plan, phases 20–26). This document
mirrors **Phase 21 — region hardening**, which executes *here*, plus the
cross-repo contract obligations.

---

## 1. pheasant's role in Synapse

Synapse is a hyperfast federated knowledge-base system. In its brain
metaphor:

- **pheasant instances are the regions**: each container owns one
  specialized knowledge base — sync engine, SQLite/FTS5 state, knowledge
  graph, self-search, MCP/HTTP surfaces. Sizes range from single-digit MB
  to multi-TB. Regions are fully self-contained and deploy standalone.
- **Pheasant Flock is the nervous system**: a router that decides
  *which regions to ask* by scoring each region's published **semantic
  contract**, fans the query out to the chosen regions' self-search, and
  merges/re-ranks the answers.
- **The semantic contract** is the artifact a region derives from its own
  content and publishes after every successful sync: embedding-space
  signature (centroid, covariance diagonal, ≤32 cluster centroids),
  concept vocabulary (from this repo's graph concepts) + MinHash,
  capability descriptor, freshness watermark. Bounded ≤ 256 KB.

Two integration invariants:

1. **The contract schema is owned by pheasant-flock.** Its Pydantic
   model is canonical; this repo vendors only the exported JSON Schema
   under `contracts/` plus golden fixtures, with a CI parity test
   (sha256 equality with the other repo's fixtures). Never hand-edit the
   vendored files.
2. **No Python dependency between the repos.** The boundary is the
   contract JSON + HTTP. A region must never import pheasant-flock;
   the router never imports pheasant. Regions must keep working with no
   router configured (all Synapse behavior is a no-op when
   `synapse.router_url` is unset).

---

## 2. Phase 21 — region hardening (executes in this repo)

These steps fix the gaps found in the 2026-06-10 audit and make a pheasant
container a production-grade region. One step per agent session; each run
writes `runs/<ts>-synapse-<step>/SUMMARY.md` (create `runs/` +
gitignore it). Steps marked **[x-repo]** require matched work in
pheasant-flock via its `pheasant-coordinator` skill — identical
branch name in both repos, both test suites green before either push.

### Step 21.1 — Real watcher + scheduler

**Gap:** `src/pheasant/sync/watcher.py` and `sync/scheduler.py` are stubs;
no live incremental sync despite `sync.watcher.*` / `sync.scheduler.*`
config keys existing in `config/schema.py`.

**Does:** implement `WatcherService` on `watchdog` observers per enabled
filesystem source: collect events, debounce (`sync.watcher.debounce_ms`),
batch, then call `SyncEngine.sync_source(name, mode="incremental")`.
Implement `SchedulerService` as a daemon-thread interval loop
(`sync.scheduler.interval_seconds`) calling `sync_all("incremental")` as
the fallback for non-filesystem sources. Both started/stopped by
`pheasant start`; both no-ops when disabled in config.

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
writer (one-shot `pheasant sync`, startup sync, watcher, scheduler, HTTP
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

**Does:** new `src/pheasant/search/vector_store.py`: `VectorStore`
protocol with a `lancedb` default backend (optional extra
`[vector]`) and a deterministic **stub embedder** for offline tests.
Embeddings are computed at sync time per chunk through an OpenAI-spec
HTTP embedding endpoint (`search.embeddings.base_url` / `model` /
`api_key_env`) — the same provider surface pheasant-flock uses, so
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
`pip install 'pheasant-kb[vector]'` hint). Idempotency bookkeeping is
content-addressed: chunk ids embed the chunk `text_hash`, so "already
embedded?" is exactly store membership — `VectorIndexer` embeds only
missing ids and prunes ids absent from the `chunks` table at sync end
(re-syncing unchanged content performs zero embedder calls, asserted in
`tests/test_sync_idempotency.py`). `StubEmbedder` hashes tokens (blake2b)
to fixed unit directions with a small synonym canonicalization table so
hybrid-vs-text acceptance runs offline. The **[x-repo]** obligation was
satisfied by wire conformance, not code: `OpenAISpecEmbedder` speaks the
standard OpenAI embeddings HTTP shape the pheasant-flock provider
layer uses, so a fleet pins one model for both repos; no sibling-repo
change was needed. `numpy` was promoted to a core dependency (the numpy
backend + stub must work without `[vector]`).

### Step 21.5 — Contract publisher + sync event stream **[x-repo]**

**Gap:** no sync-completion signal (`sync_events` is write-only); no
contract; the router has nothing to route on.

**Does:** new `src/pheasant/synapse/` package:
- `publisher.py`: on every `sync.completed`, derive the semantic contract —
  signature from 21.4 chunk vectors (streaming mean, covariance diagonal,
  seeded mini-batch k-means m ≤ 32), vocabulary from graph concept nodes
  (top ≤ 256 by weight) + 128-perm MinHash, watermark =
  sha256 over ordered source checkpoint digests, capabilities from config.
  Validate against the vendored JSON Schema, write
  `/state/contract.latest.json` (tmp+rename), serve via
  `GET /contract` (FastAPI) and MCP resource
  `pheasant://knowledge-bases/{kb_id}/contract`.
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

**Implementation note (2026-06-14, landed):** new `src/pheasant/synapse/`
package (region-side; not the sibling's `synapse/`). The contract is built **by
hand** to the vendored `contracts/semantic_contract.v1.schema.json` — no import
of pheasant-flock. Verified byte-for-byte against the sibling's canonical
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
  `pheasant://knowledge-bases/{kb_id}/contract` (+ `get_contract` tool) serve
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
`pheasant backup <out.tar.zst>` / `pheasant restore <in>` covering
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
- **Backup format.** `pheasant backup <out.tar.zst>` (new
  `persistence/backup.py`) writes a zstd-compressed tar of the durable state:
  `pheasant.db` taken via **`VACUUM INTO` a temp file** (a consistent snapshot
  under WAL — never a raw copy of the live db; manifests live in the db since
  21.2 so they ride along), plus `graphs/`, `events/`, `vectors/`, any legacy
  `manifests/` dir, and `contract.latest.json` when present. The live state dir
  is read-only during backup.
- **Restore safety.** `pheasant restore <in.tar.zst>` decompresses + extracts
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

**Phase 21 exit criterion:** a single pheasant container survives
kill-mid-sync, live-watches its sources, answers vector+hybrid
self-search offline-testably, publishes a schema-valid contract on every
sync, can be backed up and restored byte-faithfully — with the standalone
(non-fleet) mode untouched.

---

## 3. Contract obligations (quick reference)

| Obligation | Where |
|---|---|
| Vendored JSON Schema | `contracts/semantic_contract.v<N>.schema.json` (do not edit; re-vendor from pheasant-flock) |
| Golden fixtures | `contracts/fixtures/*.json` — byte-identical with the other repo (`tests/test_contract_parity.py`) |
| Publisher | `src/pheasant/synapse/publisher.py` (Step 21.5) |
| Serving | `GET /contract` + MCP resource |
| Push | `POST <router>/v1/synapse/events` on `sync.completed` |
| Embedding space | The region publishes its own `embedding_space` (model/dim) in the contract; since 2026-07-11 the router routes **heterogeneous fleets** by partitioning per space, so regions with different models/dims coexist. Only when the router opts into the `synapse.embedding_space` pin must `search.embeddings.model` equal it (HTTP 409 otherwise) |
| Signing (optional, Step 24.4) | `synapse.signing_key_ref` → `src/pheasant/synapse/signing.py` Ed25519-signs `integrity.signature`; router rejects (HTTP 403) under `require_signed` |

### Heterogeneous embedding spaces (2026-07-11, [x-repo] — router-side; pheasant docs-only)

The fleet no longer requires all regions to share one embedding model/dim.
The router (pheasant-flock, ADR 2026-07-11 in its `docs/DECISIONS.md`)
now **partitions** registered contracts by `embedding_space
(model_id, dim, normalized)` and scores each partition with a query vector in
that space (per-space query embedders under its `synapse.spaces` config, or
explicit per-space `query_vecs`); regions whose space cannot be resolved for a
query are excluded with `embedding_space_unresolved` in the routing report.
Cross-space math remains forbidden — white-matter edges are confirmed within
one space only.

**Region-side impact: none.** pheasant already embeds with its own configured
model (`search.embeddings`) and publishes its own `embedding_space` in the
contract; the **wire format is unchanged** (no schema bump, no re-vendor,
`tests/test_contract_parity.py` green). Operators may now mix regions on
local, OpenAI-spec, or Gemini(-compatible) embedding models in one fleet —
just make sure the router config carries a `synapse.spaces` entry (or a
matching default provider) for each `model_id` the fleet's regions report, so
text queries can be embedded into every space.

### Step 24.4 — Ed25519-signed contracts + A2A (2026-06-20, [x-repo])

A region can **optionally sign** its semantic contract so the router can verify
authenticity/integrity beyond the `content_hash`:

- **Config:** set `synapse.signing_key_ref` to a secret *reference*
  (`env://NAME` or a bare env-var name). The referenced value is the base64 of a
  32-byte raw Ed25519 private seed — the plaintext key never lands in YAML or on
  disk. **Unset (default) → unsigned** (`integrity.signature: null`); a
  **standalone, router-less pheasant is entirely unchanged**.
- **What gets signed:** the *exact same canonical body bytes* the
  `integrity.content_hash` covers (body with `integrity` excluded,
  `sort_keys=True`, compact separators, `ensure_ascii=False`). The signature
  lives outside the hashed body, so signing never perturbs the content hash.
  `src/pheasant/synapse/signing.py` (`sign_body`/`signing_bytes`) is the
  region-side codec; it is byte-compatible with the router's
  `SemanticContract.verify_signature`, guarded by the cross-repo signing-parity
  fixture `contracts/fixtures/signed-demo-region.v1.contract.json` (+ PARITY).
- **Out-of-band public key (decision):** the router holds the kb_id→public-key
  trust store in *its own* config (`synapse.trust.keys`) and enforces
  `synapse.require_signed`. The public key is **not** added to the contract, so
  the **contract wire format / vendored JSON Schema are unchanged** — no
  schema-version bump, no re-vendor.
- **Optional dependency:** the `cryptography` import is gated behind the new
  `[a2a]` extra (`pip install 'pheasant-kb[a2a]'`). A region without
  `signing_key_ref` needs no crypto dep; the offline suite passes without it.

### Step 25.4 session A — multi-modal: image ingest (2026-06-21, [x-repo])

A region can ingest **images** (`.png`/`.jpg`/`.jpeg`/`.webp`/`.gif`) by
**captioning** them into indexable text (architecture §8: project everything
into the *one* fleet-pinned text embedding space; modality-native vectors like
CLIP would stay region-local and are out of scope). The caption becomes the
artifact's text and flows through the normal chunk → embed → graph path like
any other document. **Image only this session — audio (transcribe-then-index)
is session B.**

- **Captioner abstraction:** `src/pheasant/ingestion/captioner.py`.
  `StubCaptioner` is the **default + offline** path (deterministic caption from
  the file name + a blake2b digest of the image bytes, so the same image always
  captions identically and different images differ; tests use it, no network /
  no decoder / no model). `OpenAISpecVisionCaptioner` is the gated production
  path — OpenAI-spec `POST {base_url}/chat/completions` with an `image_url`
  content part (data-URI base64), caption read from
  `choices[0].message.content`. An authored sidecar `<image>.caption.txt`
  always wins (offline real captions for fixtures/demos). Captioning is the
  **only** sanctioned indexing-path network call besides the 21.4 embedder, and
  like it must keep the stub path.
- **Config:** `ingestion.captioner.{provider,model,base_url,api_key_env,prompt}`
  — `provider: stub` (default) or `openai-spec`. The API key is read from the
  named env var at call time, never stored. The captioner is **only built when
  a source's `include` globs admit an image extension** (e.g. `**/*.png`), so a
  text-only region is byte-identical to pre-25.4 (no captioner, no possible
  network call). The `stub` default needs no extra dependency.
- **Idempotency:** the engine's pre-read sha256 skip
  (`_can_skip_before_read`) compares the image's content hash *before* reading
  bytes, so an unchanged image in an incremental sync is **never re-captioned**
  — the same zero-work guarantee the embedder gets (21.4).
- **Modalities wiring (contract):** the 21.5 publisher's `_capabilities()`
  appends `"image"` to `capabilities.modalities` when an image source is
  configured. The router (pheasant-flock) already filters by
  `--modality image` *before* scoring (22.1), so an image query routes only to
  image-capable regions. **`modalities` is existing contract data — the wire
  format / vendored JSON Schema are UNCHANGED** (no schema bump, no re-vendor,
  parity test green).
- **Tests:** `tests/test_image_ingestion.py` (caption searchable; artifact
  typed `image`; zero re-caption on unchanged re-sync; text-only region builds
  no captioner; contract advertises `image`). Router-filter test on the Flock side
  (`tests/synapse/test_router.py::test_modality_image_routes_only_to_image_capable_regions`).

### Step 25.4 session B — multi-modal: audio ingest (2026-06-21, [x-repo]) — COMPLETES Step 25.4 + Phase 25

A region can ingest **audio** (`.wav`/`.mp3`/`.m4a`/`.flac`/`.ogg`) by
**transcribing** it into indexable text — the audio twin of session A's image
captioning, same architecture §8 principle (project into the *one* fleet-pinned
text embedding space; modality-native audio vectors stay region-local, out of
scope). The transcript becomes the artifact's text and flows through the normal
chunk → embed → graph path. The transcriber and captioner share a tiny additive
helper `src/pheasant/ingestion/_modal.py` (`sidecar_text` + `stub_fingerprint`)
so they stay in lock-step; session A's observable behavior is unchanged.

- **Transcriber abstraction:** `src/pheasant/ingestion/transcriber.py`.
  `StubTranscriber` is the **default + offline** path (deterministic transcript
  from the file name + a blake2b digest of the audio bytes — same file always
  transcribes identically, different audio differs; tests use it, **no network /
  no audio decoder / no ASR model / no audio library**). `OpenAISpecTranscriber`
  is the gated production path — OpenAI-spec `POST {base_url}/audio/transcriptions`
  as a stdlib-urllib multipart upload (`model` + raw `file` bytes), transcript
  read from the response `text` field. An authored sidecar
  `<audio>.transcript.txt` always wins (offline real transcripts for
  fixtures/demos). Transcription is a sanctioned indexing-path network call
  alongside the 21.4 embedder and the 25.4A captioner, and like them keeps the
  stub path so the suite is network-free.
- **Config:** `ingestion.transcriber.{provider,model,base_url,api_key_env}` —
  `provider: stub` (default; `model: whisper-1`) or `openai-spec`. The API key is
  read from the named env var at call time, never stored. The transcriber is
  **only built when a source's `include` globs admit an audio extension** (e.g.
  `**/*.wav`), so a text-only / standalone region is byte-identical to pre-25.4
  (no transcriber, no possible network call). The `stub` default needs **no
  extra dependency**.
- **Idempotency:** the engine's pre-read sha256 skip (`_can_skip_before_read`)
  compares the audio's content hash *before* reading bytes, so an unchanged audio
  file in an incremental sync is **never re-transcribed** — the same zero-work
  guarantee the embedder (21.4) and image captioner (25.4A) get.
- **Modalities wiring (contract):** the 21.5 publisher's `_capabilities()`
  appends `"audio"` to `capabilities.modalities` when an audio source is
  configured. The router (pheasant-flock) already filters by
  `--modality audio` *before* scoring (22.1), so an audio query routes only to
  audio-capable regions. **`modalities` is existing contract data — the wire
  format / vendored JSON Schema are UNCHANGED** (no schema bump, no re-vendor,
  parity test green).
- **Tests:** `tests/test_audio_ingestion.py` (transcript searchable; artifact
  typed `audio`; zero re-transcribe on unchanged re-sync; text-only region builds
  no transcriber; contract advertises `audio`); fixture
  `tests/fixtures/sample_workspace/audio/briefing.wav` + `.transcript.txt`
  sidecar (a few bytes, no real decoder). Router-filter test on the Flock side
  (`tests/synapse/test_router.py::test_modality_audio_routes_only_to_audio_capable_regions`).

### Decision note 2026-08-06 — `"document"` modality (PDF/DOCX extraction)

Document text extraction (`src/pheasant/ingestion/extractor.py`) closed a gap
where `.pdf`/`.docx` were accepted by the pipeline and then produced no text at
all — see the 2026-08-06 entry in `CLAUDE.md` and
`runs/2026-08-06-pdf-extraction/SUMMARY.md`. The only Synapse-visible
consequence is one more entry in an existing contract field:

- The 21.5 publisher's `_capabilities()` appends `"document"` to
  `capabilities.modalities` when a source's `include` globs admit any of the
  **seven** extractable document extensions — `.pdf`, `.docx`, `.doc`,
  `.pptx`, `.xlsx`, `.rtf`, `.epub` — so a router's `--modality document`
  filter (22.1) routes document questions only to regions that can actually
  read them. One modality covers all seven deliberately: from the router's
  point of view "can this region read a document?" is the routable question,
  and a per-format modality (`"pptx"`, `"epub"`, …) would push format
  dispatch into the fleet contract for no routing benefit. A region that gains
  a format therefore needs **no** contract or router change.
- **`modalities` is existing contract data — the wire format / vendored JSON
  Schema are UNCHANGED** (no schema bump, no re-vendor, parity test green).
  This follows the 25.4 image/audio and 33.1 memory precedents exactly, so it
  carries **no `[x-repo]` obligation**: the router needs no change to honor it,
  because `--modality` already filters on whatever strings a contract declares.
- Extraction adds **no network call** to the indexing path — unlike the
  captioner/transcriber, every provider is offline and deterministic, so the
  rule-1 determinism guarantee is unaffected and there is nothing new to gate.
- Regions ingesting PDFs from untrusted connector sources can set
  `ingestion.extractor.provider: sandboxed` to run the PDF tokenizer inside the
  Phase-34 WASM sandbox (fuel + memory cap, zero host capabilities). This is a
  region-local hardening choice with no contract or routing impact.

## 4. Deployment notes

A region remains the existing container (`Dockerfile`, port 8765, PVC on
`/state`). In a Synapse fleet:

- **Compose** (Synapse Step 25.1, **landed 2026-06-20** in the router repo):
  the `docker compose --profile synapse` topology runs 1 router + 3 demo
  pheasant regions, each with `synapse.publish: true` +
  `synapse.router_url` → the router. **No pheasant code change** was needed:
  the region image is built unmodified from this repo's `Dockerfile` (sibling
  build context `../pheasant`, or `PHEASANT_IMAGE` pinned tag), and the three
  fleet-demo region configs + fixture workspaces are **vendored on the router
  side** (`pheasant-flock/deploy/synapse-demo/`) and mounted into the
  container at `/config/pheasant.yaml` + `/workspace`. Regions sync on startup
  (21.1) and publish their contract over the 21.5 webhook; the router's
  file-backed registry fills over HTTP (no shared volume). Standalone pheasant
  is unchanged — drop `synapse.publish`/`router_url` and the region is
  router-less again. See `pheasant-flock/docs/DEPLOY.md` §11.
- **Kubernetes** (Synapse Step 25.2, **landed 2026-06-20** in the router
  repo): the router repo's sibling Helm chart
  `pheasant-flock/deploy/helm/synapse/` renders the whole fleet — one
  router (`Deployment` + HPA + `Service`) plus a values-driven `regions:`
  list where **each entry becomes one `StatefulSet` + a `/state` PVC
  (`volumeClaimTemplate`, so each region owns its own volume — independent
  scale-up) + a headless `Service` + a `ConfigMap`**. **No pheasant code
  change** was needed: the region pod spec in the chart **mirrors this
  repo's `deploy/kubernetes/` manifests** (port 8765, `/health`+`/ready`
  probes, `/state`+`/config`+`/workspace`+`/exports` mounts,
  non-root 10001, read-only rootfs) — those manifests remain the pod-spec
  baseline; the chart vendors that shape on the router side (chart values),
  the same boundary as the 25.1 compose configs. Regions publish their
  contract to the router webhook (21.5) over the headless Service DNS name;
  standalone pheasant is unchanged. The live `helm template | kubeconform`
  + kind smoke is a runbook in `pheasant-flock/docs/DEPLOY.md` §12
  (the router-repo build env had no helm binary).
- Auth: regions accept a bearer token (`security` settings) minted by the
  router's tenancy layer; local/demo fleets may run open.

---

## 5. Phase 34 — WASM sandboxing & selective acceleration (executes in this repo)

**Status:** complete (2026-08-03). Not Synapse-contract work — no wire-format
impact, no `[x-repo]` obligation, no coordination with pheasant-flock
required. Included here per the existing convention of tracking phased
region-hardening-style work in this document. One step per agent session;
each run writes `runs/<ts>-synapse-34.N/SUMMARY.md`. Outcome summary: 34.1-34.3
(sandboxing) and 34.5 (both accelerated hot loops, production-wired, opt-in)
shipped; 34.4 (benchmark spike) produced the go/no-go data plus a Rust
toolchain now available in this environment; 34.6-34.7 were benchmarked and
correctly concluded NO-GO rather than built — see each step's entry below
and its `runs/2026-08-03-synapse-34.N/SUMMARY.md` for the full numbers.

**Why now:** concept extraction — pheasant's biggest indexing cost — was
already retired 2026-08-03 (see the Decision entry above this section; sync
1.5h → 2m53s). Any WASM initiative has to be judged against what's left, not
against that already-solved problem. Reading the actual hot paths (not
assuming) surfaces two real, unmitigated gaps:

1. **Unsandboxed third-party connector plugins.** `sync/connector_registry.py`'s
   `ep.load()` executes arbitrary Python in-process with zero isolation;
   ambient secrets (e.g. `NOTION_TOKEN`) are visible to any plugin via
   `os.environ`. No competing mitigation is in flight.
2. **Two confirmed O(graph) hot loops that scale with multi-source growth,
   unmitigated:**
   - `graph/builder.py:add_cross_source_edges` → `graph/enrichment.py:325
     resolve_cross_source_edges` walks every edge on **every sync**, full or
     incremental — references can only resolve once both sides are indexed,
     so it can't be gated on `changed_ids` the way `add_similarity_edges` is
     (`builder.py:184-209`). Fixed per-sync cost floor that rises as sources
     are added.
   - `search/graph_search.py:_scan_edges` (via `_relationship_hits`) scores
     every edge whenever a query needs relationship hits. Node search got an
     FTS5 candidate prefilter (`search/node_index.py`) after its O(nodes) scan
     hit multi-second latency on a 500k-node graph; edge search never got the
     equivalent and has no index-backed prefilter today.

Both loops are already algorithmically sound (O(V+E), not quadratic) — the
cost is pure-Python iteration over a whole-collection, once-per-sync/query
call, which is exactly the shape where the Python↔WASM FFI boundary cost is
worth paying (one marshal in, native loop, one marshal out — not per-item).

**Where WASM does *not* help, checked explicitly so it isn't relitigated:**
node-level graph search (already solved by `node_index.py`; WASM would only
help the degraded index-absent fallback), vector search at scale (the answer
is enabling the existing `LanceDBVectorStore` `[vector]` extra, not a new WASM
ANN index), ingestion-time chunking (already O(n) per file; FFI overhead
likely offsets gains at that per-item call granularity), and anything in
`persistence/`/SQLite/FTS5/MCP internals (already native-speed C code, not an
untrusted-input surface).

**Architecture (no container/topology change):** stays mono-container per
knowledge base — matches the Synapse fleet model (one region = one container,
replicated externally by the router); does not conflict with `sync/locks.py`'s
`EngineLease` (still exactly one writer process per `/state` dir). The sandbox
and both hot-loop accelerations for 34.5a run **inside the existing
`sync/worker.py` child-process indexing worker** — the seam pheasant already
uses to isolate CPU-heavy indexing from request-serving under the GIL. The
`_scan_edges` acceleration (34.5b) is the one piece on the **query** path — it
runs in the main API server process instead, with its own `wasmtime` `Store`,
and is sequenced after 34.5a proves the harness out.

**Mechanism:** `wasmtime` via `wasmtime-py`, WASI preview1
(`wasm32-wasip1`) — broader toolchain support today than the still-stabilizing
component model. New `[wasm]` extra in `pyproject.toml` (same gated-import
precedent as `[a2a]`/`[vector]`): a WASM-untouched region carries no new dep
and stays byte-identical. CPU is fuel-metered (`Config.consume_fuel`,
deterministic budget-based failure — fits the determinism ethos better than a
wall-clock timeout); memory is capped per-`Store` via `StoreLimits`
/`ResourceLimiter` (a genuinely new capability — Python has no per-call
memory cap short of process-wide `rlimit`); the host surface is
capability-scoped (no ambient WASI env/filesystem/network — the guest calls a
host-provided `host_fetch(url)`, checked against a new
`connector.allowed_hosts` allowlist, so guest code never sees raw secrets;
local file reads reuse `security/path_policy.resolve_under`, never a second
filesystem guard). A new `SandboxedConnector` tier sits **alongside** the
existing native `SourceConnector` tier, opted in per-source via
`connector.runtime: sandboxed` (default stays native) — additive-only plugin
surface (CLAUDE.md rule 8), nobody is forced to compile WASM to add a
connector.

### Step 34.1 — Host harness (empty sandbox)

**Gap:** no WASM runtime exists in the codebase at all.

**Does:** stand up `wasmtime` `Engine`/`Store`, fuel metering, a
`StoreLimits` memory cap, and the capability-scoped `host_fetch` surface — no
connector changed yet. New `[wasm]` extra.

**Acceptance:** a minimal "hello wasm" guest module runs; a fuel-exhaustion
case and a memory-cap-exceeded case both fail closed, under new tests.

**Files:** new sandbox runtime module, `pyproject.toml` (`[wasm]` extra),
`tests/test_wasm_harness.py`.

**Landed 2026-08-03.** No Rust/TinyGo toolchain was available in this
environment, so guest fixtures are hand-authored WAT compiled by
`wasmtime.Module`'s built-in parser rather than a separate build step.
Full detail: `CLAUDE.md` §5, `runs/2026-08-03-synapse-34.1/SUMMARY.md`.

### Step 34.2 — Reference sandboxed connector

**Gap:** no sandboxed connector exists to validate the harness against real
connector-shaped behavior.

**Does:** a reference sandboxed connector exercising the harness's capability
surface (file listing/reading via the host-mediated path, `host_fetch`);
`tests/fixtures/pheasant-connector-example/` is the canonical third-party
shape to match. Extend `src/pheasant/testing.py`'s `ConnectorConformance` so a
sandboxed connector is held to the identical
deterministic/idempotent/incremental contract as native connectors.

**Acceptance:** the reference sandboxed connector passes the identical
conformance suite as the native `StaticDirConnector`.

**Files:** reference guest module + loader wiring in
`sync/connector_registry.py` / `sync/connectors.py:615-617`
(`connector_for_source`), `testing.py`, `tests/test_sandboxed_connector.py`.

**Landed 2026-08-03.** Listing/read stay host-side (guarded by
`security/path_policy.resolve_under`); only the per-item content transform
runs in the guest — a deviation from a guest doing its own WASI filesystem
syscalls, rationale in `runs/2026-08-03-synapse-34.2/SUMMARY.md`.
`ConnectorConformance` needed no code changes, already connector-agnostic.

### Step 34.3 — Adversarial limit enforcement

**Gap:** 34.1's fuel/memory limits and capability scoping are untested
against actually adversarial guest behavior.

**Does:** fixtures that try to allocate unbounded memory, infinite-loop, read
ambient env vars, or call a non-allowlisted host.

**Acceptance:** every adversarial case fails closed with a clear error — no
hang, no secret leak, no partial write.

**Files:** `tests/test_wasm_adversarial.py`.

**Landed 2026-08-03.** All four adversarial shapes fail closed, verified
directly (typed exceptions, wall-clock bounds, spy-fetcher call counts,
zeroed guest memory) — `runs/2026-08-03-synapse-34.3/SUMMARY.md`. Closes
the sandboxing arc (34.1-34.3).

### Step 34.4 — Benchmark spike, priority-ordered

**Gap:** no measured data exists on whether WASM-compiled hot loops actually
beat pure Python once FFI marshaling cost is included, at realistic and
growing multi-source scale.

**Does:** measure, on a corpus at least comparable to the 2,132-file demo
corpus (`CLAUDE.md` 2026-08-03 entry), ideally larger to show the scaling
trend:
(a) WASM-compiled `resolve_cross_source_edges` vs. current pure Python, at
increasing source counts;
(b) WASM-compiled `graph_search._scan_edges` vs. current pure Python, at
increasing edge counts;
(c) *(lower priority)* WASM-compiled chunking vs. current, including FFI
marshaling cost;
(d) *(speculative)* packed-linear-memory graph representation vs.
`SimpleMultiDiGraph`'s dict-of-dicts, measuring live RAM during a sync.

**Acceptance:** numbers checked into the run summary, including how each
scales with source/edge count — not a single-point measurement. Steps
34.5-34.7 are each go/no-go based on their slice of this spike.

**Files:** `runs/<ts>-synapse-34.4/SUMMARY.md` (benchmark harness + results;
no production code changes this step).

**Landed 2026-08-03.** No Rust/TinyGo toolchain existed in this
environment either (per 34.1); installed one (`rustup`, GNU host, no MSVC
dependency) rather than hand-author the comparison logic in WAT, per an
explicit user decision — hand-writing a hash-map-backed path resolver with
Python-identical matching semantics carried a real silent-wrong-answer
risk a sandboxing fixture doesn't. Parity-verified (12/12 exact matches)
against the Python originals before trusting any timing. Result:
**34.5b (`_scan_edges`) is a clear GO** (2-8x faster, growing with scale,
every point tested); **34.5a (`resolve_cross_source_edges`) is a
CONDITIONAL GO** (loses below ~1,300-2,500 edges, breaks even almost
exactly at this repo's own 2,903-edge demo corpus). Also surfaced an
implementation-critical finding: a naive per-call-compiled sandbox is
5-20x *slower* than Python — any real integration must compile the guest
module once and reuse it. Full numbers: `runs/2026-08-03-synapse-34.4/SUMMARY.md`.

### Step 34.5 — WASM-accelerated cross-source resolution and relationship search

**Conditional on 34.4a/b** (expected go given confirmed unmitigated O(graph)
loops).

- **34.5a:** `resolve_cross_source_edges`, invoked from the indexing worker
  process (`sync/worker.py`) — same process as the connector sandbox.
- **34.5b:** `graph_search._scan_edges`, invoked from the main API server's
  query path — a separate integration point, sequenced after 34.5a.

Both keep a pure-Python fallback (feature flag; default set by the 34.4
numbers).

**Files:** `graph/builder.py`, `graph/enrichment.py`, `search/graph_search.py`,
`sync/worker.py`, config flag, tests.

**Landed 2026-08-03.** The critical implementation finding from 34.4 (naive
per-call compile is 5-20x slower) is solved with `wasmtime`'s AOT module
serialization (`Module.serialize`/`deserialize_file`) — a fresh `Config`+
`Engine`+deserialize, fully matching a cold `sync/worker.py` subprocess,
loads a precompiled artifact in under 1ms vs. ~103ms to JIT-compile.
34.5a shipped as a **full** port (both `python_import` and
`document_link`/`url` paths — a partial port matching only the 34.4 spike's
scope would have silently dropped markdown/document-link cross-source
edges behind the config flag, a correctness regression). Both accelerators
are opt-in (`graph.wasm_cross_source_resolution`,
`search.wasm_relationship_search`, default off) with a pure-Python fallback
on any failure, verified by failure-injection tests. AOT cache lives under
the OS temp dir, not any KB's `/state` — the compiled binary is
knowledge-base-**independent** (same binary for every KB; graph data is a
call argument, never baked into the module), so it is a machine-local
build cache, not KB state. Full detail:
`runs/2026-08-03-synapse-34.5/SUMMARY.md`.

### Step 34.6 — WASM-accelerated chunking

**Conditional on 34.4c** (low expected priority). Only if the spike shows a
net win after FFI overhead. Feature flag, pure-Python fallback stays default.

**Files:** `ingestion/chunking.py`, config flag, tests.

**Evaluated 2026-08-03 — NO-GO, not implemented.** Ran the missing 34.4c
slice: `chunk_text` runs once per file (not once per sync), and its
pure-Python cost (35 μs for a 2 KB file, 219 μs for 10 KB) is already
smaller than the bare fixed cost of a WASM Store+Instance alone (115 μs,
no marshal, no compute) for small/typical files — WASM loses before doing
any work. No Rust port was written: the numbers already answer it, and a
faithful port has a correctness hazard 34.5 didn't (Python slices by
Unicode code point; a naive byte-oriented Rust port risks mis-chunking
non-ASCII content). `runs/2026-08-03-synapse-34.6/SUMMARY.md`.

### Step 34.7 — Compact in-memory graph representation

**Conditional on 34.4d** (speculative). Only if the spike shows a meaningful
RAM reduction at realistic corpus sizes. Touches `graph/simple.py`'s
`SimpleMultiDiGraph` internals only — the on-disk zstd-compressed format
(`persistence/graph_store.py`) is unaffected either way.

**Evaluated 2026-08-03 — NO-GO, not implemented — completes Phase 34.** Ran
the missing 34.4d slice: live (uncompressed) `SimpleMultiDiGraph` memory via
`tracemalloc`, measured at the actual 2,132-file demo-corpus scale
(13,503 nodes / 13,502 edges → **20.5 MB**) and a 10x stress scale
(135K/135K → **205 MB**). Not a memory problem at either scale by any
reasonable container budget — the plan's own "2.3 MB compressed" reference
undersold how small this already is once measured live. No implementation
attempted; touching `SimpleMultiDiGraph` (the structure the whole indexing/
search/enrichment pipeline reads and writes) isn't justified without a real
constraint to fix. `runs/2026-08-03-synapse-34.7/SUMMARY.md`.

**Files:** `graph/simple.py`, tests.

**Risks / feasibility (checked at 34.1, not assumed):** `wasmtime-py` wheel
availability for the Dockerfile's `python:3.12-slim` target(s) (linux/amd64,
and arm64 if multi-arch); a `wasm32-wasip1` reference connector and any
accelerated hot-loop modules need a build step in CI, kept isolated from the
main Python test matrix (`tests/fixtures` is already excluded from default
pytest recursion); 34.5 has two integration points (indexing worker +
main API server) rather than one, more surface area than the sandboxing work
alone. Rule compliance throughout: no LLM calls added (rule 1); standalone
mode stays sacred (rule 7) — `[wasm]` is default-off and gated; plugin
surface evolution stays additive-only (rule 8) — sandboxed is a new tier
beside native, never a replacement.

**Verification throughout:** `pytest -q` stays green and network-free — no
step here adds a non-stub network call. Manual check at 34.1: run a
deliberately malicious fixture connector (unbounded-alloc, infinite-loop) and
confirm the parent API server keeps serving requests throughout, proving the
child-process isolation (`sync/worker.py`) plus the new sandbox actually
contains the failure. Manual check at 34.5: confirm p50/p95 query latency for
a query that triggers `_relationship_hits` improves measurably with the WASM
path enabled vs. disabled on the benchmark fixture, and that
`pheasant sync --mode incremental` on a one-file change shows a lower fixed-
cost floor with 34.5a enabled on a multi-source corpus.

---

## 6. Phase 35 — Horizontal scale, durable coordination, capacity guidance (executes in this repo)

**Status:** in progress (opened 2026-08-16). Not Synapse-contract work — no
wire-format impact, no re-vendor of `contracts/`, no `[x-repo]` obligation.
Tracked here per the existing convention for phased region-hardening work
(Phase 21 §2, Phase 34 §5). One step per agent session; each run writes
`runs/<ts>-synapse-35.N/SUMMARY.md`.

**Why now.** Running several large collections at once exposes that pheasant is
architecturally *one process, one writer, one knowledge base*: `sync/locks.py`'s
`EngineLease` permits exactly one writer per `/state` dir, the graph is a single
in-RAM zstd-JSON blob rewritten whole on every checkpoint
(`persistence/graph_store.py`), the shipped manifests pin `replicas: 1` on RWO
volumes, `/metrics` is a stub returning `pheasant_up 1`, and job progress is
per-process and in-memory (`jobs.py`) so a multi-hour first index is
indistinguishable from a hang and invisible to any other replica. The measured
baseline to beat: **8x the file workers bought 1.113x**, because commits are
serialized through one coordinator — scaling preparation was the easy half.

**Standalone stays sacred (rule 7).** Postgres, the broker and gRPC are
first-class *selectable backends*, not replacements. SQLite / in-process /
HTTP remain the defaults; a `docker run` with no infrastructure must keep
working, which is what holds the 5 MB end of the range.

| Step | What | Status |
|---|---|---|
| 35.0 | Remove the Obsidian exporter | done (2026-08-16) |
| 35.1 | Observability: real `/metrics`, per-source progress with throughput/ETA/stall detection | done (2026-08-16) |
| 35.2 | `StateBackend` seam + Postgres backend (incl. FTS5 → `tsvector` port) | done (2026-08-16) |
| 35.3 | Graph capacity measured; sharding chosen over a Postgres graph backend | done (2026-08-16) |
| 35.4 | Multi-writer indexing: per-source leases + sharding | done (2026-08-16); durable queue deferred |
| 35.5 | Durable worker dispatch, gRPC transport, and the durable index queue (NATS JetStream) | done (2026-08-16) |
| 35.6 | Process roles, serving durability, autoscaling and the three runtimes | done (2026-08-16) |
| 35.7 | Measured capacity model and sizing guidance | queued |

### Step 35.0 — Remove the Obsidian exporter (done 2026-08-16)

**Contract:** delete the vault *projection* in its entirety; keep indexing an
Obsidian vault as a *source*.

The UI's graph workspace (`/graph`, added 2026-08-07) replaced the reason the
projection existed. Removing it also halves the 35.2 seam audit:
`obsidian/exporter.py` held 8 of the 16 raw `StateStore.rows()` call sites, and
that raw-SQL escape hatch is what would otherwise defeat a backend seam.

**Removed:** the `src/pheasant/obsidian/` package;
`SyncEngine.export_obsidian_notes`; `POST /obsidian/export`; MCP
`export_obsidian_notes`; `ObsidianSettings` and the `obsidian` config section;
`PheasantSettings.vault_path`, `PheasantConfig.vault_path` and
`StatePaths.vault`; every `/vault` mount (Dockerfile, compose, k8s, Helm) and
the `/vault` entry in `security.allow_workspace_roots`;
`docs/obsidian_integration.md` and its nav entry.

**Kept:** `SourceType.obsidian_vault`, its `.obsidian/` autodetection
(`quickstart.py`, `targets.py`) and its watcher entry — indexing a vault is an
ingest feature and is unaffected.

**Acceptance:**

- A pre-removal config carrying `obsidian:` and `pheasant.vault_path` still
  loads. `PheasantConfig.model_validate` already drops unknown keys, so this
  passed *silently* before the shim; what `config/loader.warn_on_removed_settings`
  adds — and what the test pins — is that the removal is **reported**, once per
  process per key, with guidance. Mutation-tested: neutering the helper fails
  both tests.
- `pheasant up` on a `.obsidian/` fixture still detects `obsidian_vault`.
- Files already written under a `/vault` mount are user data and are left
  untouched on disk (rule 2); nothing is deleted.

**Rule 8 exception:** removing the `export_obsidian_notes` MCP tool is a
breaking change to a public surface that is otherwise evolved additively. It
was removed outright rather than deprecated because the exporter behind it is
gone. Recorded in CLAUDE.md rule 8 and `docs/mcp_tools.md`.

**Graph taxonomy:** `generated_note` is retired in `docs/graph_model.md`. It
was documented from the initial build and **never emitted** by any release, so
no persisted graph contains one — the removal cannot invalidate stored state
(rule 3 untouched: the stable-ID grammar does not change).

### Step 35.1 — Observability (done 2026-08-16)

**Contract:** make "is the indexer moving?" and "what does the autoscaler scale
on?" answerable. Both were unanswerable: `/metrics` returned the literal string
`pheasant_up 1`, and `jobs.py` tracked one phase string and one counter per
*job* — so a `sync_all` over eight sources hid the one that was stuck behind the
seven that were fine.

**Metrics.** New `src/pheasant/telemetry/metrics.py`: counters, gauges and
histograms rendering Prometheus exposition text, hand-rolled rather than
depending on `prometheus-client`. The reason is not dependency asceticism — it
is that the part of that library people need is a cross-process registry, and
that is exactly the part which cannot work here: indexing runs in a **child
process** (`sync/worker.py`), so in-process counters there die with the child
regardless of library. Indexing series are therefore gauges sampled at scrape
time from live job state (`JobRegistry.metrics_sample`), not counters. Label
values are escaped and metric names validated at registration, because one
malformed series breaks an entire scrape rather than just itself.

**Per-source progress.** `SourceProgress` per source inside a job; `JobProgress`
becomes a derived rollup, kept so pre-35.1 callers and the UI keep working. The
rollup's `total` stays `None` until *every* source knows its own — a partial sum
shrinks the denominator as sources report in and runs the bar backwards.
Throughput and ETA are **observed** server-side over a sliding window
(`RATE_SAMPLES`), not reported by the indexer: the wire is unchanged, and a
caller that emits no rate still gets one. A phase change clears the denominator
and the rate window, because counters are phase-local (files while preparing,
chunks while embedding) and carrying them across produces a plausible, false bar.

**Slow vs stuck.** `seconds_since_progress` is always reported so a client can
say "last update 4s ago" during healthy work; `stalled` only trips after
`STALL_AFTER_SECONDS` (300s, deliberately generous — a large PDF or a
rate-limited embedding batch is slow, not stuck) and is styled as a warning, not
a failure.

**Wire.** `ProgressHook` gains an optional fifth `meta` argument carrying
`source` plus counters. `_hook_accepts_meta` decides by **signature**, once per
wrap, which form to call — not by catching `TypeError` around the call, which
would misread a `TypeError` raised *inside* a four-argument hook as an arity
mismatch and silently re-invoke it. Four-argument callbacks stay supported and
are tested. `sync_all`'s `[source]` prose prefix is gone: the source is now a
field. The CLI's NDJSON emitter throttles **per source**, since one global
counter let a fast source suppress every update from a slow one.

**Surfaces.** `_with_sync_state` (the single overlay every source-listing route
already passed through) gains `progress` — this source's slice, not the whole
run's. UI: `SyncProgress.tsx` is shared by the jobs tray and the Sources page so
the two cannot disagree about what "stalled" looks like.

**Acceptance:** `tests/test_observability.py` (23). Mutation-tested 8/8 caught
**after** closing a gap the first run exposed: every test passed `source=`
explicitly, so neutering the source-less fallback changed nothing and the whole
back-compat branch was uncovered. Two tests now cover it. Suite **1057 passed /
27 skipped** (+23). Docs: `docs/how-to/monitor-indexing.md`.

**Deferred deliberately — job persistence (was 35.1c) moves to 35.4.** The plan
had jobs persisted to the state store this step. Persisting them alone is a
*partial* answer that is arguably worse than none: on restart the sync itself is
gone (it was a subprocess), so a persisted record would show a job that is no
longer running. The useful version is a job that can be **resumed**, which needs
the durable queue and per-source leases of 35.4. Recorded rather than dropped.

**Known gap, reported not hidden:** `failed`/`failures` are in the model and
always empty, because a file that fails to prepare raises and ends the whole
pass (`_prepared_items` calls `future.result()` with no per-item tolerance).
Per-item fault tolerance and a dead-letter queue are 35.4; the fields exist
because that is where those counts will land. Documented as a caveat in
`docs/how-to/monitor-indexing.md` rather than left for a reader to discover.

### Step 35.2 — StateBackend seam + Postgres (2026-08-16)

**Done:** the seam (`persistence/sql.py`, `backends.py`, `schema.py`,
`secrets.py`), the Postgres backend, the portable schema, the Postgres arms of
`search/sqlite_store.py` and `search/node_index.py`, `storage.backend/dsn_env/
pool_size`, the `[postgres]` extra, and `tests/test_backend_parity.py`
(7 tests, skipping cleanly with no DSN). Verified against a real PostgreSQL 16
cluster throughout.

**The planning estimate was wrong by 7x.** The plan said ~8 raw-SQL call sites;
there are **57 across 15 modules**, plus **76** `self.conn` uses inside
`StateStore` including ten `with self.conn:` transaction blocks. That changed
the approach, not the scope: the SQL is almost entirely ANSI, so translating at
one boundary (plus a `sqlite3.Connection`-shaped adapter) is far safer than 57
and 76 individually-edited lines on the default path.

**Proven identical across backends:** artifact/chunk/FTS/node/edge counts,
**byte-identical stable IDs** (rule 3), and a zero-work second sync (rule 4).
`chunks_fts` stays a real table with the same columns on Postgres, so the whole
write path is untouched.

**Ranking: three real gaps found and fixed, one structural residual left.**
The first port used a bare `ts_rank_cd` and ranked badly. Three separate causes,
each fixed:

1. **No IDF at all.** `ts_rank_cd` has no notion of term rarity, so a decoy
   repeating a common query word outranked the one document containing the rare
   one. The rank is now built the way BM25 is — a **sum over query terms of
   `IDF x that term's rank`** — with document frequencies fetched in one indexed
   round trip per query (`_postgres_document_frequencies`) and the BM25 `+1`
   IDF variant, chosen because the classic form goes negative for terms in over
   half the corpus.
2. **No term-frequency saturation.** `ts_rank_cd` grows near-linearly with
   occurrences where BM25 saturates (`tf/(tf+k1)`), so linear growth outran a
   2.6x IDF difference. Normalization flag **32** (`rank/(rank+1)`) supplies it;
   measured against flags 0/1/33, which all rank the decoy first.
3. **Tokenizer mismatch — the one that mattered most.** FTS5's `unicode61`
   splits `deploy-gateway.md` into `deploy`/`gateway`/`md`; Postgres's `simple`
   dictionary keeps it as **one lexeme**, so a search for "deploy" did not match
   the file *named* for it at all — silently, with a plausible-looking result
   list. The generated `search_vector` now flattens punctuation to spaces first.

**Residual, pinned not hidden:** BM25 normalizes each column by *that column's*
length; `ts_rank_cd` normalizes the whole weighted vector once. So when a title
match on a common term competes with body matches on rare terms, ranks 2-3 can
differ. Verified that no title:body weight ratio fixes both cases — 8:1, 16:1
and 33:1 each merely move the failure to the other query. Top-1 agrees on the
gold set. `POSTGRES_RANK_RESIDUAL` +
`test_known_residual_postgres_lacks_per_column_length_normalization` pin it, and
go red the day a `pg_search`/ParadeDB backend supplies real per-column
normalization.

**Migration:** `pheasant migrate --to postgres` copies every table, rebuilds
`chunks_fts` for the target dialect (never copies it — its tokenization is
dialect-specific), verifies row counts, and only then renames the SQLite file
`*.migrated`. Idempotent, and it never deletes the original (rule 2). Stable IDs
carry over byte-identically, so no re-index.

**Found by mutation testing, and worth recording:** the first parity corpus gave
every query a unique winning token, so it ranked correctly under *any* column
weighting — flattening the ts_rank weights, reversing them, and stripping the
title weighting out of the generated tsvector all still passed. 3 of 4 mutants
survived. The corpus now contains a document named for its query and a longer
decoy that repeats it, which is what surfaced the IDF gap at all.

**Acceptance:** `tests/test_backend_parity.py` (9), skipping cleanly without
`PHEASANT_TEST_POSTGRES_DSN`. Docs: `configuration.md` §storage.backend.
**Step 35.2 is complete.**

### Step 35.3 — Graph capacity, and why sharding won (2026-08-16)

**The step changed shape because the measurement came first.** The plan was a
`GraphBackend` protocol with a Postgres arm. Measuring what the graph actually
costs said that would solve the wrong problem.

`src/pheasant/graph/capacity.py` builds a realistically-shaped
`SimpleMultiDiGraph` at four scales and reports RSS, persisted bytes, checkpoint
and load time, and edge-scan latency:

| Corpus | Nodes | RSS | Checkpoint | Load | Edge scan |
|---|---|---|---|---|---|
| 2,000 files | 12.6k | 34 MB | 0.13 s | 0.10 s | 1.8 ms |
| 20,000 | 126k | 314 MB | 1.3 s | 0.9 s | 16 ms |
| 100,000 | 630k | 1.5 GB | 6.7 s | 4.6 s | 95 ms |
| 250,000 | 1.58M | 4.3 GB | 19.9 s | 11.2 s | 240 ms |

Cost per node is flat (~2.4 KB RSS), so the table interpolates. It does **not**
extrapolate safely: a linear projection to 250k predicted 3.8 GB against a
measured 4.3 GB, 12% low — which is why the top row was measured rather than
derived.

**Correction (2026-08-16, Step 35.4):** the "33% at 250k" figure below was
**wrong**. It assumed the interval stayed at 60 s; the engine already
self-throttled to `max(60s, last_save * 10)`, capping overhead at ~10%. The
conclusion — shard rather than build a graph backend — is unchanged, because it
rests on RAM and on linear growth, not on the checkpoint figure. 35.4 replaces
the 10x rule with Young's formula and takes overhead to ~1%.

**~~The binding constraint is not RAM.~~** `storage.graph_checkpoint_seconds`
defaults to 60 s and a checkpoint serializes the whole graph: ~~11% of the
interval at 100k files, 33% at 250k~~ (see the correction above). A
Postgres graph backend would lower steady-state RSS and would **not** fix that —
the graph is still assembled in memory during a sync and still has to be
written. Sharding fixes both, because both costs divide by the shard count and
the shards index in parallel.

So: **no `GraphBackend` protocol, no Postgres graph tables.** Building them
would have added a second graph implementation to keep correct for a benefit
sharding already delivers. Recorded as a decision, not an omission.

**What shipped instead:** the capacity harness, and `graph.max_nodes` (default
750,000 nodes ≈ 120,000 files) which warns once per sync with the projected RSS
and the advice to shard. A **notice, not a refusal** — `sync.limits` can refuse
because it checks before any work happens; by the time this fires the index
exists.

**Found while measuring:** the shipped Kubernetes manifest and Helm chart both
cap memory at **1 Gi**, which covers about **65,000 files**. Past that the pod
is OOM-killed mid-sync and it presents as an unexplained restart, not as a
capacity problem. Documented in `capacity-planning.md`.

**Acceptance:** `tests/test_graph_capacity.py` (6), mutation-tested 5/5 caught
— including one asserting the default threshold and the published table cannot
drift apart, since a warning that fires at a size the doc calls fine is worse
than no warning. Docs: `docs/how-to/capacity-planning.md` (sizing per corpus
size, the checkpoint ceiling, how to choose a shard boundary, and which storage
backend), plus `configuration.md` §graph.max_nodes.

### Step 35.4 (partial) — Checkpoint scheduling and container sizing (2026-08-16)

**Correction first.** Step 35.3 reported checkpoint overhead as "33% of a 60 s
interval at 250k files". That was wrong: the engine already self-throttled to
`max(60s, last_save * 10)`, so the interval stretched and overhead was capped
near **10%**, not 33%. 35.3's conclusion — shard rather than build a graph
backend — is unaffected, because it rests on RAM and linear growth rather than
on that figure.

**Checkpointing now follows Young's formula** (`T = sqrt(2 x C x MTBF)`), the
standard HPC and ML-training result, replacing the 10x rule. It minimizes the
*sum* of checkpoint overhead and work redone after a crash rather than either
alone:

| Corpus | Cost | Old interval / overhead | Young / overhead | Worst-case rework |
|---|---|---|---|---|
| 2,000 | 0.13 s | 60 s / 0.2% | 150 s / 0.1% | 2.5 min |
| 20,000 | 1.3 s | 60 s / 2.2% | 8 min / 0.3% | 8 min |
| 100,000 | 6.7 s | 67 s / 10.0% | 18 min / 0.6% | 18 min |
| 250,000 | 19.9 s | 199 s / 10.0% | 31 min / 1.1% | 31 min |

An order of magnitude less overhead. The trade is bounded rework, capped by the
new `storage.graph_checkpoint_max_seconds` (30 min). The operator-facing input
is `storage.checkpoint_mtbf_seconds` (24h) — the one number an operator
actually knows — and lowering it on spot/preemptible instances makes pheasant
checkpoint more often, which is exactly where that pays.

Degrades to the previous behaviour when it cannot compute: no measured save yet
(the first checkpoint of a fresh graph) or MTBF switched off both return the
plain floor. Returning 0 there would checkpoint on every artifact.

**Container sizing.** `deploy/kubernetes/deployment.yaml` and the Helm chart go
from **1 Gi → 6 Gi** (and 1 → 2 CPU), covering ~350,000 files instead of
~65,000. `graph.max_nodes` moves with it — 750k → **1.5M nodes (~240k files)**,
the point at which a 6 Gi container is genuinely full given the graph is ~60% of
process RSS. A threshold that does not match the container it ships with either
nags early or never fires before the OOM kill, so a test asserts the default,
the published table and both manifests agree.

**Acceptance:** `tests/test_graph_capacity.py` 6 → **10**, mutation-tested 5/5
caught (including reverting to the 10x rule, dropping the ceiling, and drifting
`max_nodes` away from the manifest).

**Still queued for 35.4:** per-source write leases on Postgres, the durable work
queue with dead-lettering, and `pheasant shard plan`.

### Step 35.4 (rest) — Per-source leases and shard planning (2026-08-16)

**The ceiling is lifted.** `SourceLease` (`sync/locks.py`) holds a write lease
per *source* in the state database, so two indexers can commit `docs` and
`code` concurrently — the thing the 35.2 Postgres seam was built for. Exclusion
is a single conditional `INSERT … ON CONFLICT DO UPDATE … RETURNING`, so the
database arbitrates. **On SQLite nothing changes**: one writer per file is an
accurate model there, not a limitation to route around, and `EngineLease` still
enforces it.

**`pheasant shard plan`** turns the 35.3 recommendation into a proposal. It
scans each source (a walk, not an index) and packs **whole sources**
largest-first. Whole sources is the load-bearing decision: hashing paths across
shards balances perfectly and severs every `references`/`imports` edge, which
is the opposite of what the graph is for. Even balance is the wrong objective
when the thing being balanced is a graph. LPT lands within 4/3 of optimal, and
optimal is both NP-hard and an answer to a size *estimate*.

**Two test bugs of mine, both found by the tests failing:**

* The concurrency test released each lease in `finally`, so a winner deleted
  the row before the next thread tried — six threads acquiring *serially*, not
  contending. It reported two winners and looked exactly like a lease race. Now
  a `threading.Barrier` starts them together and winners hold.
* The packing test named its sources so that alphabetical order equalled
  descending size, making "largest-first" and "in arrival order"
  indistinguishable. Mutation testing caught it: replacing the sort changed
  nothing. The fixture now sorts ascending by name, and the assertion is on
  balance rather than on a specific assignment.

`try_acquire` gained `RETURNING` regardless: the original read-then-write did
have a real window between claiming and checking, even though that is not what
the flaky test was measuring.

**Acceptance:** `tests/test_sharding_and_leases.py` (14), mutation-tested 5/5
caught. Suite **1076 passed / 41 skipped**; 23 pass against real Postgres.

**Deferred: the durable work queue with dead-lettering.** Per-source leases
already let N indexers work in parallel — a queue adds *dispatch* (who picks up
what, retries, poison-message handling), which matters when indexers are
autoscaled and disposable. That is 35.6's problem, and building the queue
before the roles that consume it would be building it against a guess.

### Step 35.5 — Durable dispatch, gRPC, and the index work queue (2026-08-16)

**The gap, stated plainly.** `file_executor: remote` picked a worker with
`position % len(urls)` and sent one `urlopen` per file. Every failure mode a
distributed system actually has was unhandled: one dead worker failed *its
share of every sync*, a rolling deploy did the same, a slow worker had no
deadline, and a 50,000-file source paid 50,000 TCP — over TLS, 50,000
handshake — round trips.

**Durability is the bulk of the value, and it is not configurable.** Policy
lives in `sync/worker_pool.py`, bytes in `sync/worker_transport.py`, and the
split is what lets a second transport inherit every property rather than
reimplement it: keep-alive pooling, batching, bounded retry with **full**
jitter honouring `Retry-After`, a per-endpoint circuit breaker, failover across
endpoints and then to **local preparation**, health feeding
`pheasant_worker_up{endpoint}`, deadline propagation, and content-addressed
idempotency keys (the sha256 was already computed before dispatch).

Two policy decisions worth naming:

* **A refusal is not a failover.** HTTP 422 / gRPC `INVALID_ARGUMENT` means the
  worker understood the task and declined it; every worker runs the same
  refusal logic, so trying four of them spends four round trips to learn one
  thing. The coordinator prepares locally, which accepts everything the remote
  path deliberately does not. A refusal also does not count against the
  breaker, or one bad file would take a healthy worker offline.
* **When every endpoint is open, the honest answer is none.** The first draft
  probed the least-recently-tripped endpoint anyway, "because a breaker is a
  heuristic" — which spends `MAX_ATTEMPTS` round trips *per batch* rediscovering
  that the fleet is down, the exact expense the breaker exists to stop. It now
  returns nothing and the caller prepares locally until the cooldown lets one
  probe through.

Remote preparation is a *throughput* optimization, so it can no longer fail a
sync. That is asserted rather than claimed: a sync with every worker dead
produces byte-identical artifacts to one with every worker healthy.

**Worker side.** `POST /internal/indexing/prepare-batch` honours the caller's
remaining deadline (stopping *between* tasks rather than finishing work nobody
is waiting for) and answers a duplicate idempotency key from a bounded LRU
cache instead of re-parsing. A worker that predates the route answers 404 once;
the coordinator remembers and uses the single-task path, so a coordinator
upgraded ahead of its fleet keeps working.

**gRPC (`[grpc]` extra, `sync.concurrency.worker_transport: http|grpc`).** What
it buys, measured rather than assumed: file content crosses as protobuf `bytes`
instead of base64 — a flat 33% saving on the only large field — and
`PrepareBatch` is bidirectional, so one refused file no longer fails the whole
batch the way a single JSON body must. **No generated code is checked in:**
modern `protoc` output calls `ValidateProtobufRuntimeVersion` with the exact
runtime it was built against, so a vendored stub pins every installer to one
protobuf release, and its generated `import pheasant_worker_pb2` is not
package-relative and would not resolve inside the package anyway. The `.proto`
is compiled at import time via `grpc.protos_and_services`, which is why
`grpcio-tools` is a runtime dep of the extra. Structured fields stay JSON
*inside* the proto so a config change is not a proto change; only the payload
is binary. A missing extra raises rather than silently falling back to HTTP —
an operator who asked for gRPC and got JSON would keep paying the inflation
they chose gRPC to avoid.

**The durable work queue (`sync.queue`), deferred from 35.4 and now built.**
The deferral said a queue "adds dispatch, which matters when indexers are
autoscaled and disposable" — but the in-memory list has a failure that does not
wait for 35.6: a process killed nine sources into ten has *lost* the tenth,
nothing outside that process can see the backlog, and there is no number for a
scheduler to scale on. So the queue lands here with `local` (the state store
itself — no broker, works on SQLite and Postgres) and `nats` (JetStream,
`[queue]` extra) backends, and 35.6's `--role indexer` becomes the same drain
loop with an idle timeout. **Off by default**: with `sync.queue.enabled: false`
`sync_all` is unchanged, and a test asserts not one row is written.

**Three real bugs, each found by a test rather than by review:**

1. **The claim was a write running through the read path.** `LocalQueue.claim`
   used `StateStore.rows()` for its `UPDATE … RETURNING`, which never commits —
   so on SQLite the WAL write lock was held against every other claimant (the
   suite went from 0.6 s to *hanging*) and, far worse for a queue, the claim was
   invisible to another process, which is the one property the whole thing
   exists to have. New `StateStore.execute_returning` commits.
2. **A genuine double-claim race on Postgres**, found by a test written
   *because* mutation testing showed the SQLite suite could not distinguish the
   guard. My reasoning had been wrong: under READ COMMITTED only the **outer**
   WHERE is re-evaluated after the winner commits, and this queue deliberately
   allows claiming an `inflight` row (that is how a dead worker's task is
   redelivered) — so `status IN (pending, inflight)` is *still true* against the
   winner's own write and both transactions claimed the row. Repeating
   `visible_at<=?` in the outer clause is what falsifies it. SQLite serializes
   writers, so no arrangement of guards fails there.
3. **`depth()` on NATS read the stream, not the consumer.** A stream's message
   count does not drop on ack under the default retention, so the autoscaling
   gauge would only ever grow — an HPA that scales up and never down. It reads
   the durable consumer's `num_pending`/`num_ack_pending` now, and the consumer
   is created on connect so a backlog with no worker yet does not read zero.

**Surfaces:** `pheasant queue status|drain|requeue-dead`,
`pheasant worker --transport grpc`, `pheasant_index_queue_depth` /
`_inflight` / `_dead_letters` from the queue when it is on, and a
`sync.queue` block in `pheasant.example.yaml`, `docs/configuration.md` and the
setup wizard.

**Acceptance:** `tests/test_worker_durability.py` (40),
`tests/test_grpc_worker.py` (21, every one against a real gRPC server on a
loopback port), `tests/test_index_queue.py` (29, including six against a real
NATS JetStream broker and one Postgres concurrency test). Mutation-tested
**28 mutants, 27 caught**; the one survivor is the outer `status` guard, which
covers an ack landing between the subquery and the update — real, free, and
not deterministically forceable without two hand-driven transactions, so it is
recorded in the module docstring as uncovered rather than left looking
load-bearing.

**Not done, and deliberately:** the queue has no consumer role of its own yet
(`sync_all` publishes and drains in one process), and nothing autoscales on the
depth gauge. Both are 35.6.

### Step 35.6 — Roles, serving durability, and the three runtimes (2026-08-16)

**The gap.** `pheasant serve` does everything: API, MCP, UI, watcher,
scheduler, indexing. Right for one container and wrong for several — and the
reason is not performance. Three replicas of that process against one
knowledge base all watch the same directories, all fire the same scheduled
sync, and all try to index the same source. The 35.4 leases keep that from
corrupting anything, but three processes taking turns to do one process's work
is not horizontal scale.

**Roles** (`deployment/roles.py`, `pheasant serve --role`) state which jobs a
process has, as data with a test asserting the whole table. `all` is the
default and is unchanged; `api` **never indexes** and publishes to the queue
instead, which is the cell that makes the replica count free; `indexer` drains
the queue; `worker` prepares. `all` deliberately does *not* drain — a single
container turns the queue on for crash resumption, not to join a fleet.

Because `api` publishes rather than runs, the queue is a hard requirement for
it: `--role api` without `sync.queue.enabled` refuses at startup rather than
accepting syncs that vanish, and a `wait: true` sync returns **409** naming
the fix. Routes are deliberately *not* hidden per role: the Service selector
is what keeps traffic off an indexer, and a role whose `/search` 404s is
harder to debug than one with no clients.

**A correctness gap the role split exposed, found by asking what an api
replica actually reads.** The knowledge graph is a *file*. A process loads it
once at startup, and the only reload path (`reload_graph`) runs after a sync
**that process** performed — so an api replica, which never indexes, would
answer graph queries from whatever the graph was when its pod started.
Indefinitely, and silently: text and vector search read the shared database
and stay current, which is exactly what makes it easy to miss. Api replicas
now poll the file's mtime/size (`server.api.graph_refresh_seconds`, 30 s) and
reload when it changes. Shipping fleet manifests without closing this would
have shipped a fleet that quietly disagrees with itself.

**Serving durability** (`deployment/serving.py`), both **off by default**
because both are replica behaviors:

* **Shed rather than queue.** With one process there is nowhere for a shed
  request to go, so waiting is the best available answer and a 429 to the only
  user is worse. With N replicas a fast 429 lets the load balancer try
  another. `/health`, `/ready` and `/metrics` are never shed — a pod that 429s
  its own liveness probe gets **restarted** by the thing protecting it, which
  is strictly worse than the overload.
* **Drain before dying.** Kubernetes removes endpoints and sends SIGTERM
  *concurrently*, and propagation is not instant, so a process that exits
  promptly drops what was routed to it in the gap. `drain_seconds` fails
  readiness first, keeps serving, then shuts down. The handler **chains** to
  uvicorn's rather than replacing it — replacing it would drain and never
  exit — and is installed from the lifespan, because uvicorn installs its own
  inside `run()` and a handler installed earlier is simply overwritten. A
  second SIGTERM skips the wait.

`stateless_http=True` is now pinned by a test. It is what makes MCP replicas
safe — two requests from one agent may land on different replicas — and it was
true and undocumented since the MCP server was written.

**Manifests.** `deploy/kubernetes/scaled/` is a three-tier fleet: api
Deployment (HPA on CPU, PDB `minAvailable: 1`), indexer StatefulSet (one
replica, **not** autoscaled, PDB `minAvailable: 0` so node drains are not
blocked forever), worker Deployment (KEDA on `pheasant_index_queue_depth`,
scaling to **zero**; plain-HPA fallback included). `docker-compose.scale.yml`
is the same shape with `--scale worker=N`. The single-container install and
`docker-compose.yml` are untouched and still need no infrastructure.

Two requirements are stated rather than assumed, because both would otherwise
fail confusingly on someone else's cluster: **Postgres** (SQLite is one writer
per file, and not one file across pods) and a **ReadWriteMany** `/state`
volume (the graph is a file api replicas read; RWO attaches to one node).

**Acceptance:** `tests/test_process_roles.py` (28),
`tests/test_serving_durability.py` (20), `tests/test_fleet_manifests.py` (20).
The manifest tests check the coupling nothing else would: that every `--role`
in `args` is a role this code knows, that each embedded config passes the same
`validate_role` the process runs at startup, that `drain_seconds` is shorter
than `terminationGracePeriodSeconds` (two numbers in two files), that workers
never receive the database DSN, and that the autoscaler's metric name still
exists in the registry. `docker compose config` validates the Compose file.
Mutation-tested **23/23 caught** across the three areas.

**Not done, deliberately:** no `kubeconform`/cluster validation of the
manifests (that belongs in CI with a schema bundle, not in an offline suite),
and nothing here is measured under real load — 35.7 is where numbers belong.
