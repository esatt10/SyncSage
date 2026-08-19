# CLAUDE.md — pheasant

Context hand-off for any agent working on this repository. Read it first; it is
intentionally dense. It describes the system **as it is now**. Anything not
here should be derivable from the code, `docs/`, or a single grep — and where
docs and code disagree, **the code is authoritative**.

---

## 1. What this project is

**pheasant** is a Docker-first, local-first **MCP context server** that turns
configured sources (git repositories, folders, single files, Obsidian vaults,
web collections, SaaS connectors, API/S3) into a queryable **knowledge graph**
with hybrid self-search, for agents and humans.

Design pillars — these are product guarantees, not preferences:

1. **Idempotent indexing** — re-syncing unchanged content produces the same
   state (content sha256 + stable IDs).
2. **Incremental by default** — connector checkpoints and manifests skip
   unchanged artifacts. A second sync of an untouched corpus does no work.
3. **Deterministic parsing** — no LLM calls in the indexing path; all
   enrichment is rule-based and reproducible. The only sanctioned network calls
   at sync time are the optional embedder, captioner and transcriber, and each
   keeps a stub/offline path so `pytest` stays network-free.
4. **Persistence split** — `/state` (operational truth: SQLite or Postgres,
   graph, manifests) and `/exports` (regenerable payloads). State dirs are
   **user data**.

### 1.1 pheasant's second role: a Synapse brain region

pheasant is also the **region** component of **Synapse**, a federated
knowledge-base system whose router lives in the sibling **pheasant-flock** repo.
Each container publishes a **semantic contract** derived from its own content;
the router scores contracts to decide which regions to query and fans out to
each region's self-search.

Two iron rules: the contract schema is canonical in pheasant-flock — this repo
only vendors the exported JSON Schema and fixtures under `contracts/` — and
there is **no Python dependency between the repos**. The boundary is contract
JSON over HTTP, and a router-less pheasant must keep working unchanged. See
`docs/SYNAPSE_INTEGRATION.md`.

---

## 2. Repository layout

```
pheasant-kb/
├── CLAUDE.md · AGENTS.md      ← agent hand-off (this file is canonical)
├── README.md                  ← product front door
├── pyproject.toml             ← extras: mcp, vector, agent, a2a, wasm,
│                                postgres, grpc, queue, docs, dev
├── pheasant.example.yaml      ← reference config, every section
├── Dockerfile                 ← one universal image: API + MCP + UI, port 8765
├── docker-compose*.yml        ← default / override / fresh reset / scaled
├── contracts/                 ← VENDORED Synapse schema + fixtures (never edit)
├── deploy/                    ← kubernetes/ (+ scaled/), helm/, compose/
├── docs/                      ← MkDocs Material site (see mkdocs.yml nav)
├── examples/                  ← demo-agent-framework, vscode MCP config
├── scripts/                   ← release_version.py, sync_version.py
├── src/pheasant/
│   ├── cli.py                 ← up/host/setup/mount/start/serve/worker/sync/
│   │                            scan/queue/shard/migrate/backup/restore/
│   │                            export/mcp/…
│   ├── setup_wizard.py        ← `pheasant setup`, defaults read off the schema
│   ├── quickstart.py          ← `pheasant up` config generation
│   ├── capacity.py            ← the one home for sizing coefficients
│   ├── analytics.py           ← Parquet exports + the DuckDB query surface
│   ├── sharding.py            ← `pheasant shard plan`
│   ├── jobs.py                ← per-source progress: phase, rate, ETA, stalled
│   ├── config/                ← schema.py (dataclasses), loader, profiles
│   ├── sync/                  ← engine, connectors, watcher, scheduler, locks,
│   │                            queue, worker_pool, worker_transport, grpc
│   ├── connectors/            ← first-party SDK plugins: notion, gdrive,
│   │                            slack, confluence, imap
│   ├── ingestion/             ← pipeline, chunking, content_types, taxonomy,
│   │                            extractor (7 doc formats), captioner,
│   │                            transcriber, office, msdoc
│   ├── graph/                 ← model, simple, builder, enrichment, capacity
│   ├── search/                ← sqlite_store (FTS5/tsvector + BM25),
│   │                            graph_search, hybrid (RRF), criteria, vector
│   ├── memory/                ← store, projection, policy, steering, salience,
│   │                            bridge, maintenance, benchmark
│   ├── persistence/           ← state_store, backends (sqlite|postgres),
│   │                            schema, graph_store, manifest, migrate, paths
│   ├── mcp_server/            ← server.py (FastMCP), tools.py (PheasantTools)
│   ├── api/app.py             ← the HTTP surface
│   ├── assistant/             ← grounded answering + workflows
│   ├── sandbox/               ← WASM runtime, sandboxed connector, accel/
│   ├── deployment/            ← roles, serving durability, mounts, host
│   ├── security/              ← path_policy, acl, idp
│   ├── synapse/               ← contract publisher, events, signing
│   └── telemetry/metrics.py   ← Prometheus exposition
├── ui/                        ← React + Vite workspace (baked into the image)
└── tests/                     ← 87 pytest modules, offline by design
```

Key entities: **knowledge base** (`kb_id` = `pheasant.name`) → **sources** →
**artifacts** (stable ID `file:{source}:{relpath}:branch={b}`) → **chunks**
(+ full-text index) and graph nodes (symbol / entity / heading / memory_record /
external_reference) with edges (contains / has_chunk / has_heading / mentions /
references / imports / calls / similar_to / supersedes / about). Full grammar:
`docs/graph_model.md`.

---

## 3. Canonical commands

```bash
pip install -e ".[dev,mcp]"
pytest -q                                  # offline by design
ruff check src tests && ruff format --check src tests
mkdocs build --strict                      # needs the [docs] extra

pheasant up [PATH...]                      # detect → config → index → serve
pheasant setup [--advanced|--accept-defaults|--answers F]
pheasant host ~/notes                      # config + compose file, then run it
pheasant mount <host-path> [--at /data/x]  # bind-mount + allow-list it
pheasant scan                              # project RAM/disk/time before indexing
pheasant validate && pheasant doctor
pheasant sync --source <name> --mode incremental|full|validate_only|repair
pheasant serve --role api|indexer|worker|all
pheasant worker                            # stateless preparation worker
pheasant queue status|drain|requeue-dead
pheasant shard plan                        # split a corpus across regions
pheasant migrate --to postgres             # one-shot, verified, preserves original
pheasant backup|restore
pheasant export parquet [--table NAME]      # /exports/parquet/<kb_id>/*.parquet
pheasant export query "SELECT …"            # SQL over an export directory
pheasant export tables [--schema]           # what is exportable; --schema for columns
pheasant mcp --transport stdio
pheasant client-config claude-code|cursor|vscode
pheasant config show                       # resolved config after profile+YAML+--set

docker compose up                          # container + optional UI profile
docker compose -f docker-compose.scale.yml up --scale worker=3
```

---

## 4. Rules

1. **Never put an LLM call in the indexing path.** Determinism is a product
   guarantee. The optional embedder, captioner and transcriber must each keep a
   stub path so the suite stays network-free.
2. **Treat `/state` as user data.** Schema/layout changes ship a one-shot
   idempotent migration that preserves originals (`*.migrated` rename, never
   delete). New tables arrive via `CREATE TABLE IF NOT EXISTS` in `SCHEMA`.
3. **Stable IDs are contracts.** Changing the ID grammar in
   `docs/graph_model.md` breaks every persisted graph — it needs a migration
   and an explicit decision note.
4. **Idempotency tests are the spine.** `tests/test_sync_idempotency.py` must
   stay green; any sync change adds cases there.
5. **Keep house style:** argparse CLI, dataclass config schema, ruff
   format + lint, pytest, Python ≥ 3.11, type hints.
6. **Never import pheasant-flock.** The Synapse boundary is contract JSON over
   HTTP. Never hand-edit vendored files under `contracts/`.
7. **Standalone mode is sacred.** Every change must leave a router-less,
   infrastructure-free pheasant fully functional. Postgres, gRPC, the broker
   and the role split are *selectable backends*; SQLite, HTTP, no queue and
   `--role all` are the defaults, and each seam owes a test asserting the
   no-infrastructure path is unchanged.
8. **The MCP tool surface is public API.** Renaming or removing a tool breaks
   deployed agents — additive evolution only, deprecate before remove. One
   sanctioned exception to date: `export_obsidian_notes` was removed outright
   when the exporter behind it was deleted, because there was nothing left for
   the tool to do. That is a precedent for "the feature is gone", not for
   renames.
9. **Cross-repo work** (anything the Synapse spec marks `[x-repo]`) uses
   identical branch names in both repos, contract fixture parity (sha256), and
   both test suites green before either push.
10. **Verify against the real thing.** Postgres, NATS, wasmtime and a real
    container have each caught bugs a mock could not. When a change touches a
    backend, run that backend. When it touches the image, build the image.
11. **Config-schema changes owe the config surface an update.** Adding a
    *top-level* section to `src/pheasant/config/schema.py` needs three things:
    a mention in `docs/configuration.md`, a `Section` in
    `src/pheasant/setup_wizard.py`, and an entry in `LIVE_APPLICABLE_SECTIONS`
    (`api/app.py`) saying whether a running server can pick the change up.
    `tests/test_config_surface_freshness.py` fails CI on all three,
    mechanically. **Individual field defaults need no second edit** — the
    wizard reads them off the live dataclasses.
12. **DuckDB is read-side only.** `src/pheasant/analytics.py` uses it as a
    Parquet writer and a query engine over `/exports`; it must never become a
    `storage.backend` or appear on the sync path. Three reasons, each measured
    or documented: the write path is single-row OLTP (per-artifact `DELETE` +
    re-`INSERT`, conditional-`UPDATE` lease claims, `UPDATE … RETURNING` queue
    claims), which is a bulk-columnar engine's worst case; DuckDB's FTS index
    is rebuilt wholesale rather than maintained, which would break pillar 2;
    and its exclusive *file* lock blocks other processes from opening the
    database at all, where SQLite's WAL is what lets `docker-compose.scale.yml`
    mount `/state:ro` on the API replicas while the indexer writes. The export
    takes no lease and issues nothing but `SELECT`, which is what makes it safe
    to run during a sync.

---

## 5. How the system works now

### Ingestion

A connector lists items and reads their bytes; the engine skips anything whose
sha256 is unchanged **before** reading it, which is what makes a re-sync free.
Text is parsed by content type, chunked, and written to the state store and the
full-text index; the graph builder adds nodes and edges; enrichment resolves
cross-source references.

**Seven document formats** extract real text: `.pdf`, `.docx`, `.pptx`,
`.xlsx`, `.doc`, `.rtf`, `.epub`. Providers are `auto` (default), `native`,
`builtin` (stdlib only) and `sandboxed` (the PDF tokenizer inside a WASM guest
with no host imports). `DOCUMENT_EXTENSIONS` and `EXTRACTED_EXTENSIONS` are
asserted set-equal — that drift is exactly how a format gets accepted and then
silently indexed as nothing.

**Images and audio** are captioned/transcribed into indexable text that flows
through the normal path. Both default to a deterministic offline stub, and an
authored `<file>.caption.txt` / `.transcript.txt` sidecar always wins.

**Structural taxonomy** (`ingestion/taxonomy.py`) is opt-in per source. Six
rules detect headings across mixed conventions, ordinals are parsed and
reconciled so a document's two spellings of "four" are one number, and chunks
are cut at section boundaries so one chunk is one section.

**Connectors** resolve by `sources[].type` through entry points, so a
third-party plugin needs no dispatch code here. Five ship first-party: Notion,
Google Drive, Slack, Confluence, IMAP. `pheasant.testing.ConnectorConformance`
is the public quality bar.

### Retrieval

Three arms — text (BM25 over an FTS5 or `tsvector` index), vector (LanceDB),
and graph — fused by **reciprocal rank fusion**, because the arms score on
incomparable scales and raw-score merging silently degraded to text-only.

Ranking carries deliberate structure: `chunks_fts.title` holds the file's
**basename** with BM25 column weights `8/3/2/1`, and structural priors divide
by path depth and by tests/samples membership. Query expansion drops framing
stopwords. Criteria (`source_name`, `exclude_sources`, `node_types`,
`min_score`, `section`, `principal`) are available identically on MCP and HTTP.

Concept extraction was **retired**: it was 87% of nodes and 98.6% of edges and
failed every test set for it. `graph.enrichment._add_concept` is a no-op whose
docstring carries the measurements.

### Memory

Memory records are **source content** — one frontmatter Markdown file per
record, indexed by the ordinary pipeline. Recall *is* search. On top of that:

- **Validity** — a correction supersedes rather than overwrites, and validity
  is filtered at query time. `as_of` deliberately brings the old record back.
- **Policy** — one `MemoryPolicy` (`mode`, `scopes`, `subject`, `current_only`,
  `as_of`, `max_results`, `include_rules`) spelled identically on MCP and HTTP,
  with `sql_predicate` and `admits` as two encodings of one rule.
- **Steering** — `alias`, `preference` and `exclusion` records change ranking
  for queries that return no memory at all. Steering records are excluded from
  result lists by default: an agent asking for code should not get a line of
  rule syntax dressed as retrieved knowledge.
- **Isolation** — `normalize_acl` keys on scope: `org` is shared, `user` and
  `session` are readable only by their writer.
- **Graph** — records get `memory_record` nodes, `supersedes` edges, and
  `about` edges via a precedence ladder (reference → symbol → heading →
  entity), capped at three targets.

### Scale

One container until it shouldn't be. Then three independent axes:

| Axis | Mechanism | Scales on |
|---|---|---|
| Request traffic | `serve --role api` replicas; publish instead of index | CPU / RPS |
| Ingest throughput | `--role indexer` claiming from a durable queue, `--role worker` preparing | `pheasant_index_queue_depth` |
| Corpus size | `pheasant shard plan` packs **whole sources** per region | graph nodes |

Selectable backends, dependency-free side first: `storage.backend`
sqlite|postgres, `sync.queue.backend` off|local|nats,
`sync.concurrency.worker_transport` http|grpc.

Service-to-service traffic is durable by construction: pooled keep-alive
connections, batching, full-jitter retry honouring `Retry-After`, a per-endpoint
circuit breaker whose half-open slot admits one probe, failover to another
worker and then to **local preparation**, deadline propagation applied to the
live socket, content-addressed idempotency keys, and heartbeats that extend a
claim while the handler runs. Remote preparation is an *optimization*: no
arrangement of worker failures may change what a sync produces.

Serving durability: bounded request concurrency answering `429` +
`Retry-After` under saturation, and a SIGTERM drain that fails readiness and
keeps serving on a timer thread — never by sleeping on the event loop.

`src/pheasant/capacity.py` is the single home for sizing coefficients so
`pheasant scan`, `pheasant shard plan` and the docs cannot disagree.

### Sandboxing

Third-party connector plugins can run under `connector.runtime: sandboxed`:
one wasmtime guest per instance with deterministic fuel metering, a
linear-memory cap, and a capability-scoped host-fetch pair. A guest declaring an
import the sandbox never wires fails to **load at all**. Two hot loops
(`resolve_cross_source_edges`, `_scan_edges`) have opt-in WASM accelerators that
fall back to pure Python on any error — acceleration is a performance path,
never a correctness dependency.

---

## 6. Traps this codebase has already fallen into

Each of these cost real time. They are listed because the shape recurs.

- **An UNINDEXED FTS5 column in a `WHERE` clause is a full table scan.** A
  per-artifact `DELETE FROM chunks_fts WHERE artifact_id=?` made indexing
  O(N²); 8,000 files got 6.3× faster once it was skipped on full syncs. Treat
  the pattern as a smell.
- **Under Postgres READ COMMITTED only the *outer* `WHERE` is re-evaluated**
  after a blocking UPDATE's winner commits — not the subquery. The outer clause
  must be a predicate the winner's own write falsifies.
- **`wasmtime.Trap` and `wasmtime.WasmtimeError` are siblings**, not parent and
  child. Catch `guest_failures()`.
- **A mutation harness must `touch` the restored file and purge
  `__pycache__`.** A same-byte-length mutant restored within one mtime tick
  leaves Python running the mutated bytecode, producing a false CAUGHT.
- **A mutant that survives is a question, not a score.** Most survivors here
  turned out to be real test gaps or vacuous tests. One was correct to survive
  and is recorded as uncovered in its module docstring.
- **Test the parser you ship.** A hand-rolled `yaml.py` at the repo root
  shadowed the declared PyYAML for anything run from a checkout, so the suite
  validated against a different parser than the image used. It concealed four
  bugs before it was deleted.
- **`model_dump(mode="json")` must emit plain types.** A `str` *subclass*
  (`PluginSourceType`) is not plain data, and PyYAML's representer dispatches on
  the exact type.
- **Signal handlers run on the event loop.** Sleeping in one stops the process
  answering anything, including the readiness probe the drain exists to flip.
- **A live run finds what the suite cannot.** Container-only bugs have surfaced
  four separate times — read-only mounts, cross-process registry visibility,
  a crash on real-world malformed input. Run the real thing.
- **Deleting a `sys.path` hack breaks whatever was quietly relying on it.**
  The root `sitecustomize.py` put `src/` on the path for anything started from
  a checkout, so CI's container job ran `python -m pheasant` without ever
  installing the package. Removing the shim (right call) turned that into
  "No module named pheasant" in a job whose name says *container*, and the
  publish workflow then skipped silently because it only fires on green CI.
- **A version reference nothing rewrites is a version reference that rots.**
  `sync_version.py` rewrote the files it happened to list, so every compose
  file and manifest added later started outside the net — `docker-compose.fresh.yml`
  sat three releases behind on a tag users actually pulled. The list of files
  to stage in the release commit is now derived from the script, not pasted
  into the workflow, and `tests/test_version_alignment.py` scans the files
  themselves rather than trusting either list.
- **An image tag the docs name must be a tag something pushes.** The README's
  headline `docker run … ghcr.io/esatt10/pheasant` means `:latest`, and only
  the UI image had ever published one.
- **Record a release only after the registry has it.** The publish job used to
  commit the new version to `main` — into every compose file and manifest —
  before logging in to GHCR, so a failed push left main naming an image that
  never existed. Pushing first inverts the failure into a harmless one: an
  unrecorded image is skipped, because the next increment is computed from
  `max(pyproject, highest published tag)`.
- **A release covers every commit since the last one, not just the last PR.**
  The increment was resolved from `workflow_run.head_sha` alone, so a merge
  that a red CI left unpublished contributed nothing to the version even
  though its code shipped in the next image — a `minor` silently became a
  `patch`. The range is now every commit since the last `chore: release`, and
  the strongest increment among those PRs wins.
- **A skipped job is invisible; a failing one is not.** `container.yml` gated
  its whole job on `workflow_run.conclusion == 'success'`, so a red CI on main
  published nothing and reported it as a *skipped* run — indistinguishable
  from having nothing to do. #52 sat live on main with no image for four days
  behind that skip.

---

## 7. Pointers

- **Architecture:** `docs/architecture.md` · **Graph taxonomy:**
  `docs/graph_model.md` · **Config:** `docs/configuration.md`
- **Setup:** `docs/how-to/setup.md` (the wizard is `src/pheasant/setup_wizard.py`)
- **MCP:** `docs/mcp_tools.md`, `docs/mcp_client.md` ·
  **HTTP:** `docs/reference/http-api.md`
- **Scale:** `docs/how-to/capacity-planning.md`,
  `docs/how-to/worker-fleet.md`, `docs/how-to/indexing-performance.md`
- **Analytics/exports:** `docs/how-to/parquet-exports.md`,
  `docs/reference/export-schema.md` (the contract an outside reader gets)
  — `/exports` is a PVC/named volume an outside reader mounts; nothing is
  served over HTTP
- **Memory:** `docs/memory-system.md`, `docs/how-to/agent-memory.md`
- **Synapse region spec:** `docs/SYNAPSE_INTEGRATION.md`
- **Deployment:** `docs/deployment.md`, `deploy/kubernetes/`
