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
