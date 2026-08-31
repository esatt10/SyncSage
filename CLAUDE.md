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
│   ├── evalset.py             ← de-identified eval cases from the ledger
│   ├── evaluation/            ← the evaluation plane: contracts, snapshots,
│   │                            proof, cohorts, variants, replay, metrics,
│   │                            gates, report, candidates, runner, store,
│   │                            benchmark (the capacity measurement)
│   ├── sharding.py            ← `pheasant shard plan`
│   ├── jobs.py                ← per-source progress: phase, rate, ETA, stalled
│   ├── config/                ← schema.py (dataclasses), loader, profiles
│   ├── sync/                  ← engine, connectors, watcher, scheduler, locks,
│   │                            queue, log_queue, worker_pool,
│   │                            worker_transport, grpc
│   ├── connectors/            ← first-party SDK plugins: notion, gdrive,
│   │                            slack, confluence, imap
│   ├── ingestion/             ← pipeline, chunking, content_types, taxonomy,
│   │                            extractor (7 doc formats), captioner,
│   │                            transcriber, office, msdoc
│   ├── graph/                 ← model, simple, builder, enrichment, capacity
│   ├── search/                ← sqlite_store (FTS5/tsvector + BM25),
│   │                            graph_search, hybrid (RRF), criteria, vector
│   ├── memory/                ← store, projection, policy, steering, salience,
│   │                            bridge, maintenance, formation, benchmark
│   ├── persistence/           ← state_store, backends (sqlite|postgres),
│   │                            schema, graph_store, manifest, migrate, paths
│   ├── mcp_server/            ← server.py (MCPServer), tools.py (PheasantTools)
│   ├── api/app.py             ← the HTTP surface
│   ├── assistant/             ← grounded answering + workflows
│   ├── sandbox/               ← WASM runtime, sandboxed connector, accel/
│   ├── deployment/            ← roles, serving durability, mounts, host
│   ├── security/              ← path_policy, acl, idp
│   ├── synapse/               ← contract publisher, events, signing
│   └── telemetry/             ← metrics.py (Prometheus exposition),
│                                interactions.py (the observation plane)
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
pheasant eval bootstrap                     # de-identified eval cases from real traffic
pheasant eval taxonomy                     # the evidence taxonomy: what each event licenses
pheasant eval proof --query … --target … --event explicit_accept
pheasant eval run [--mode current_state|historical --as-of T]
pheasant eval report [--run ID] [--json]
pheasant eval trend --metric known_positive_reciprocal_rank
pheasant eval status [--watch]             # a batch's live phase/progress, from /state
python -m pheasant.evaluation.benchmark    # measure a batch against the capacity model
pheasant mcp --transport stdio
pheasant client-config claude-code|cursor|vscode
pheasant config show                       # resolved config after profile+YAML+--set

docker compose --env-file .env -f deploy/compose/docker-compose.yml up
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up --scale indexer=1 --scale worker=4
```

For deployment/configuration work, load
`.agents/skills/pheasant-deploy/SKILL.md` before changing files or containers.

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
    database at all, where SQLite's WAL is what lets `deploy/compose/docker-compose.scale.yml`
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
- **Observation** (`observability.interactions`, off) — every API/MCP call
  becomes a **row with a retention policy**: never a file, never chunked,
  never indexed, never returned by a search. A UI session's chat does not
  become knowledge because it was observed. The only path from here into
  memory is a candidate that something *admits*, and admission goes through
  `MemoryStore.append` like every other write, so invariant 1 never bends.
  Dimensioned by identity / session / modality (`ui|mcp|a2a|cli`) / criteria.
  Trace and timestamp are guaranteed, not best-effort: `NOT NULL` in the
  schema, rejected-and-counted before the insert if absent, `duration_ms`
  always set and taken from a **monotonic** clock while `started_at` is wall
  clock. The trace is ambient for the call and injected into every hop
  pheasant makes of its own — the graph-query call, remote preparation, and
  `index_tasks.payload` (attached *after* the id digest, or the content-
  addressed dedup that makes two replicas enqueue one task would break).
  `docs/memory-formation.md`.
- **Formation** (`memory.formation`, off) — deterministic rules read the
  observation plane and produce memory. `session-digest-v1` is the first:
  one record per `(session, principal)`, refined by **superseding itself**,
  so `current_only` returns exactly one and `as_of` reads the session's
  history. Written automatically rather than proposed, and only because of
  scope: `session` scope + `written_by` means only its own writer can read
  it and it decays with `session_ttl_days` — it never becomes shared
  knowledge, which still takes an explicit promotion. Two guards keep a
  repeat pass free: a text short-circuit (cheap) and the store's own id
  dedup (sound, because `supersedes` is deliberately absent from the id
  digest). Three further rules **propose** rather than write:
  `alias-cooccurrence-v1` (a query word absent from everything it retrieved,
  guarded against inflections — `coordination -> check` was a real false
  positive), `path-affinity-v1` (prefix cut at a directory boundary) and
  `retrieval-gap-v1` (a gap is *no results*, never a score threshold: fused
  RRF scores have no absolute scale). A candidate crosses into memory only
  through `MemoryStore.append`; a rejection is permanent, because
  re-suggesting what someone declined makes a review queue worth ignoring.

### Evaluation

`evaluation.*`, off by default and read-only when on. A **third plane**:
observations are evidence, records are memory, and *measurements are neither* —
nothing the `evaluation_*` tables hold is a file, is chunked, is indexed, or is
returned by a search. A region must not answer a question with its own report.

- **Typed proof, or none.** Served/considered/included are **unknown**, weight
  zero; only a caller can say `cited`/`selected`/`explicit_accept`/
  `explicit_reject`/`downstream_*`/`deterministic_validation_*`. `not_selected`
  is unknown too — the reader may have found the answer at rank one, and
  treating silence as a negative manufactures negatives at exactly the rate the
  region serves results. Weight is a product of four **reported** multipliers.
  Positive and negative sums never cancel: `P`, `N`, `Net` and a conflict rate
  are all published.
- **Snapshot manifests** digest every input that can change retrieval (content,
  sources, graph, lexical/vector index, encoding, chunking, fusion, arm limits,
  memory, steering, ACL, evaluation policy). Computed identically on any
  replica, so two pods agree on a snapshot id without coordinating. **No clock
  in either id**: a snapshot addresses state and a run addresses
  `(state, config, mode, described instant)`, so two runs over an unchanged
  region are one run and one trend point. The clock-seeded version made runs a
  second apart two rows and runs *within* a second collapse into one.
- **Six cohorts.** anchor (frozen, the trend line), rolling, **learned**
  (queries that created the memory — *recall of learned experience*, never
  reported as generalization), **temporal holdout** (later, independent
  queries), control (no steering rule can fire), synthetic invariants.
  `generalization_gap = learned − holdout` is the memorization detector.
- **Paired ablations** `B0`–`B6`. `B0` (corpus-only) is not removable: every
  attribution number is a difference against it. `B2`–`B4` hold memory
  *content* off so a retrieved record cannot be counted as a rule's doing.
- **Every metric carries its denominator, formula, substituted calculation,
  operands, proof ids, exclusions and one limitation** — `MetricResult.validate()`
  withholds one that cannot. A missing input yields `insufficient_evidence`
  with `value: None`, never `0.0`.
- **Gates are not metrics.** ACL leak, stale-current leak, `as_of` correctness,
  abstention, known-positive exclusion, control regression and negative-exposure
  increase are evaluated *before* aggregation so a good score cannot offset them.
- **Candidates are shadowed.** A proposed steering rule is passed into the
  search call for the length of one query via `extra_steering_records` — the
  real `parse_rule`/`admits` path, nothing written. A proposed *fact* is
  `not_shadow_replayable` (its text is in no index; scoring it would measure
  string similarity). Promotion needs every gate, independent queries, and a
  holdout result: `allow_originating_query_only_promotion` is off, which is
  what keeps the self-rewarding loop closed.
- **Fleet-safe.** A run claims the `__evaluation__` lease in `source_leases`
  (N replicas → one run), **never** takes `sync_lock`, and the replay searcher
  is built with `usage_tracking=False` so evaluation cannot inflate the salience
  of the records it measures. Auto-trigger fires only where the scheduler runs.
- **Progress is a row, not a process.** `phase`, unit counters and a heartbeat
  live on `evaluation_runs`, so the UI, the CLI (`pheasant eval status
  [--watch]`), HTTP (`/evaluation/status`) and MCP (`get_evaluation_status`)
  all watch a batch none of them started — across a restart. A run whose
  heartbeat expires is reclaimed as **`interrupted`** (at API boot and on the
  beat), never left spinning.
- **A batch resumes rather than restarting.** Each (cohort, variant) replay is
  checkpointed to `evaluation_replays` as it finishes; the content-addressed
  run id makes a re-run load them and replay only what is missing. Checkpoints
  clear only *after* the report commits, and a resumed run computes numbers
  identical to an uninterrupted one — asserted by killing one of two identical
  regions mid-batch and diffing health vectors.
- **Sized, not guessed.** `capacity.project_evaluation` is the one home for
  evaluation coefficients; `pheasant scan` prints run time, steady-state and
  *peak* volume separately (the peak is checkpoints in flight, the number that
  decides whether a PVC fills mid-run). `python -m pheasant.evaluation.benchmark`
  measures a real batch against the model and CI publishes the comparison —
  the first two coefficients shipped were out by 2x and 3x, found exactly that
  way. `docs/knowledge-effectiveness.md`.

### Scale

One container until it shouldn't be. Then four independent axes:

| Axis | Mechanism | Scales on |
|---|---|---|
| Request traffic | `serve --role api` replicas; publish instead of index | CPU / RPS |
| Ingest throughput | `--role indexer` claiming from a durable queue, `--role worker` preparing | `pheasant_index_queue_depth` |
| Corpus size | `pheasant shard plan` packs **whole sources** per region | graph nodes |
| Observation volume | `--role logger` draining its **own** queue (`log_tasks`, never `index_tasks`) | `pheasant_log_queue_depth` |

Selectable backends, dependency-free side first: `storage.backend`
sqlite|postgres, `sync.queue.backend` off|local|nats,
`sync.concurrency.worker_transport` http|grpc,
`observability.interactions.queue.backend` off|local|nats.

The fourth axis rises with **request traffic, not corpus churn**, which is why
it is a separate queue and a separate role rather than a `kind` column: sharing
`index_tasks` would put request-rate churn on the index claim path. Two things
it must keep true. **The request path only appends to a bounded ring** — a
ledger write per request puts a database write on the same Postgres the lexical
arm already contends on (`docs/architecture.md`'s measured bottleneck). **The
hot→cold roll never runs under `sync_lock`**, which the scheduler beat holds
across all its work; a multi-million-row Parquet write there stalls incremental
sync for every source. Under pressure the tier drops observations rather than
slowing a request, so formation thresholds count a stream thinned under load: a
busy region forms memory more slowly, not incorrectly.

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
- **A declared FK a maintenance path deliberately violates is fine under
  SQLite and fatal under Postgres.** `memory_records` carried
  `FOREIGN KEY (artifact_id) REFERENCES artifacts(id)`; `delete_artifacts`/
  `delete_source_artifacts` delete the `artifacts` row while intentionally
  leaving the `memory_records` row (preserving earned `uses`/`salience`/
  `observations` — `replace_memory_records` rebuilds the row anyway).
  SQLite never enforces a declared FK (no `PRAGMA foreign_keys=ON`
  anywhere); Postgres enforces every one by default and aborted the whole
  transaction. Two siblings, found the same way: `PostgresBackend.statement()`
  discarded `cursor.rowcount`, so any caller reading it (`subsume_records`,
  `delete_artifacts`) raised `AttributeError`; and one `INSERT OR IGNORE`
  (SQLite-only) needed `INSERT … ON CONFLICT … DO NOTHING`, the portable
  form already used everywhere else in this file. None of the three
  surfaced in the offline suite; all three surfaced in one run against a
  real local Postgres server, first try (`tests/test_backend_parity.py`).
- **A dedup that reports success must dedup into something still reachable.**
  Memory reinforcement folded a write into whatever row carried its
  canonical key, answering `created=False` / `outcome="reinforced"` — "we
  already hold this". When that row was *superseded* or compaction-*demoted*
  it was true of the counter and false of the store: the assertion was
  unreachable through every default query while the caller believed it was
  recorded. `supersede_retention_days` widened the window from one scheduler
  beat to days, which is what turned a latent edge into a live one. The rule
  now: a fold only ever targets a record a default query can return —
  corrected claims become new records, demoted ones redirect through
  `subsumed_by` — and the fold's validity predicate is spelled *exactly* as
  `MemoryPolicy.sql_predicate` spells it, empty-string corner included.
- **A ratio derived from a public enum measures the enum, not the thing.**
  `pheasant_memory_reinforcement_ratio` was computed from
  `writes_total{outcome}`, but `outcome` is public API and deliberately does
  not distinguish "folded a paraphrase" (what reinforcement newly does) from
  "folded a byte-identical repeat" (free since long before it). With the
  feature *on*, exact repeats report `reinforced`, so the gauge counted the
  thing its own docstring said it excluded, and its test passed only by
  fabricating a state the default config cannot produce. A derived metric
  needs its own inputs at its own granularity, and its test needs the real
  write path.
- **A batch insert makes one bad row cost every good row beside it.** A
  queued batch of observations is written inside one transaction, so a single
  event carrying a null `trace_id` — a truncated spool line, a garbled
  payload — raised `IntegrityError` and rolled back the whole batch. The batch
  then nacked, retried, failed identically and dead-lettered: one bad line for
  hundreds of good observations. Validation has to happen *before* the
  statement (`InteractionEvent.is_writable`), because a rolled-back
  transaction cannot drop the one bad row and keep the rest. Found by a test
  written for the batch path, not by reading it.
- **Telemetry ids that are minted twice name two different calls.** The
  interaction ledger mints W3C trace/span ids itself, because they are a row's
  primary key and must exist without the `[otel]` extra. With the extra
  installed the SDK mints its own — so the row and the exported span
  disagreed, and an operator correlating a slow span in their collector to a
  ledger row found nothing, which is most of the reason to export spans at
  all. The span starts first now and the row adopts *its* ids. Caught by
  running against a real SDK, not by the offline suite, which had no opinion.
- **Progress that lives in a process disappears with the process.** An
  evaluation batch is minutes of work, and the first version put its progress
  in the in-memory job registry. That answers neither case that actually
  happens: a browser talking to an API replica that did not start the run, and
  a reader coming back after the container was restarted — where the row also
  said `running` forever, because nothing rewrites a row when a process is
  killed. Phase, counters and a heartbeat are columns now, reclamation runs at
  API boot and on the beat, and each (cohort, variant) replay is checkpointed
  as it finishes so a restart resumes. The same shape as `source_leases`, and
  for the same reason.
- **An index in `CORE_SCHEMA` that names a column a guarded ALTER adds runs
  first, and fails the whole migration.** `CREATE TABLE IF NOT EXISTS` no-ops
  against an existing table, so `idx_evaluation_runs_live` on
  `heartbeat_at` broke every `migrate()` over a `/state` written before that
  column existed — "no such column: heartbeat_at", on boot. It is created in
  `migrate()` after the ALTER now, exactly where `idx_memory_records_canon_key`
  already was for exactly this reason. Found by pointing the CLI at an older
  state directory, not by reading the code.
- **Two staleness clocks over one dead process will disagree, and the gap is
  a feature that lies.** A killed evaluation container releases nothing, so its
  run row *and* its `__evaluation__` lease row are both left behind — and they
  aged out on different windows: `evaluation.run_stale_seconds` for the run,
  `locks.SOURCE_STALE_SECONDS` (45s) for the lease. Set the first below the
  second — the CI region uses 20s so its smoke test need not wait out 90 — and
  the region reports a batch `interrupted`, which invites a resume and says so
  in the UI, then *skips* the resume because a lease nobody was holding claimed
  a live writer. The skip was not loud either: a skipped run carries no gates,
  and `all([])` is `True`, so it reported its gates passed — which
  `pheasant eval run` turns straight into an exit status. Reclamation frees the
  lease on its own evidence and in its own window now (the staleness test lives
  in the `DELETE`, so a legitimate successor survives), and `gates_passed`
  requires a non-empty gate list. Every in-process test killed a batch by
  *raising*, which unwinds the lease's `__exit__` and releases it, so the
  offline suite could not see this: it took a real `docker compose stop`.
- **A capacity coefficient nobody measures is a coefficient that rots.** The
  first two evaluation constants were guesses: seconds-per-replay was 2x over,
  and bytes-per-checkpoint 3x *under* — the dangerous direction, since that is
  the number deciding whether a volume fills mid-run.
  `python -m pheasant.evaluation.benchmark` runs a real batch and prints
  measured beside projected; CI publishes the diff. Same posture as
  `SECONDS_PER_1K_FILES`, which was a curve being quoted as a line until
  someone measured it at two scales.
- **A measurement derived from what a system chose to show measures its own
  confidence.** Mining "appeared at rank 1" out of the interaction ledger as a
  positive would produce a retrieval metric that improves whenever ranking gets
  more *confident*, regardless of whether it gets more correct — and every
  experiment run against it confirms itself. The ledger yields `served` only:
  polarity unknown, weight zero. Utility proof has to come from a surface where
  somebody said so. The same shape one level up: a replay that counted as a
  memory *use* would let the evaluation raise the salience of the records it is
  measuring, which is why the replay searcher is built with
  `usage_tracking=False`.
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
  file and manifest added later started outside the net — `deploy/compose/docker-compose.fresh.yml`
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
- **A security default that keys off a value you override is not a default
  you have.** MCP SDK 2.x moved transport configuration off the server
  constructor and onto `streamable_http_app()`/`run()`, and with it the rule
  that auto-enables DNS-rebinding protection — which fires only when the bind
  address is loopback. 1.x read that from the *constructor's* own default, so
  pheasant got the guard whatever it bound; 2.x reads the real address, and
  pheasant binds `0.0.0.0`. The mechanical port compiled, served, and
  answered every client — with host checking silently off in every container
  deployment. `_transport_security()` now always builds the settings object
  explicitly. Caught by `tests/test_api_ui_routes.py` asserting an unlisted
  host still gets **421**, which is the only assertion in the suite that
  fails when the guard disappears.
- **An SDK that sorts your exceptions is deciding what your agents can read.**
  MCP SDK 2.x forwards the text of a deliberate `ToolError`/`ResourceError`
  and reports everything else as a bare "Error executing tool <name>", the
  exception's own text kept server-side — right for a crash. 1.x appended
  every exception's text regardless. `PheasantTools` refuses deliberately and
  informatively ("Unknown knowledge base: x", "Unknown source: y", the whole
  `PathPolicyError` remedy) but does it with plain `ValueError`/`KeyError`,
  because it is the HTTP surface's facade too and must not import the SDK. So
  the mechanical port blanked the reason on every refusal across 27 tools and
  11 resources at once: an agent that mistyped a source name was told only
  that something failed. `server.py` translates the anticipated types at the
  SDK boundary — and per *surface*, since `ToolError` raised inside a resource
  handler is stripped exactly like a crash. Found by walking every tool
  against a fake corpus and diffing the refusals against the 1.x server; no
  test had ever asserted on error *text*, so nothing went red.

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
- **Observation & formation:** `docs/memory-formation.md` — the two planes,
  the log tier, and the two combination designs that were rejected
- **Evaluation:** `docs/knowledge-effectiveness.md` — the evidence taxonomy,
  the cohort split, the ablation matrix, the gates, and what it refuses to claim
- **Synapse region spec:** `docs/SYNAPSE_INTEGRATION.md`
- **Deployment:** `docs/deployment.md`, `deploy/kubernetes/`
