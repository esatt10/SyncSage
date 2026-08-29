"""The state schema, per dialect (Phase 35.2).

The table definitions are **shared verbatim** between SQLite and Postgres —
same names, same columns, same constraints — so every one of the ~57 raw SQL
call sites and, more importantly, every stable ID built from these rows is
identical whichever backend is running. Only the type spellings differ, and
only where SQLite's affinities have no Postgres equivalent.

Full-text search is the one genuine divergence. SQLite uses FTS5 virtual
tables; Postgres has no such thing. The port keeps ``chunks_fts`` as a real
table with the *same columns*, so every write in
:mod:`pheasant.persistence.state_store` — the per-artifact ``INSERT``, the
``DELETE … WHERE artifact_id=?``, the source-wide delete — runs unchanged, and
adds a generated ``search_vector`` column plus a GIN index. Only the *query*
side differs, and that lives in :mod:`pheasant.search.sqlite_store`.

The column weights are the load-bearing detail. SQLite ranks with
``bm25(chunks_fts, 8, 3, 2, 1)`` over title/path/heading_path/text — weights
measured in the 2026-08-03 retrieval overhaul that took MRR from 0.230 to
0.594. Postgres's ``setweight`` has exactly four classes, A-D, which is a
lucky fit: title→A, path→B, heading_path→C, text→D preserves the *ordering* of
the four fields' importance. It does not reproduce BM25's arithmetic, and
nothing here pretends it does — ``tests/test_backend_parity.py`` gates on
measured retrieval quality rather than on scores matching.
"""

from __future__ import annotations

import re

from pheasant.persistence.sql import Dialect

#: Tables and indexes, shared by every backend.
CORE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS pheasant_schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_bases (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  config_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  knowledge_base_id TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  last_indexed_at TEXT,
  last_status TEXT,
  FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  relative_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER,
  sha256 TEXT,
  mtime TEXT,
  git_branch TEXT,
  git_commit TEXT,
  last_indexed_at TEXT,
  status TEXT,
  FOREIGN KEY (source_id) REFERENCES sources(id)
);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  heading_path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  summary TEXT,
  token_estimate INTEGER,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
  FOREIGN KEY (source_id) REFERENCES sources(id)
);
-- Same per-artifact DELETE pattern as artifact_terms; see that index's
-- comment.
CREATE INDEX IF NOT EXISTS idx_chunks_artifact_id ON chunks(artifact_id);
CREATE TABLE IF NOT EXISTS symbols (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  language TEXT,
  symbol_type TEXT,
  name TEXT,
  qualified_name TEXT,
  start_line INTEGER,
  end_line INTEGER,
  signature TEXT,
  docstring_summary TEXT,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
-- Same per-artifact DELETE pattern as artifact_terms; see that index's
-- comment.
CREATE INDEX IF NOT EXISTS idx_symbols_artifact_id ON symbols(artifact_id);
CREATE TABLE IF NOT EXISTS artifact_terms (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  node_type TEXT NOT NULL,
  term TEXT NOT NULL,
  normalized_term TEXT NOT NULL,
  weight REAL NOT NULL,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
-- Without this, `DELETE FROM artifact_terms WHERE artifact_id=?` (run once
-- per artifact on every sync, in replace_artifact_enrichment) is a full
-- table scan. On a table that grows past a million rows over a real sync,
-- that turns a full-corpus sync into O(n^2): each artifact's delete gets
-- slower as the table grows. Measured cause of a 2,132-file sync taking
-- 1.5+ hours.
CREATE INDEX IF NOT EXISTS idx_artifact_terms_artifact_id
  ON artifact_terms(artifact_id);
-- Retained for the historical concept rows: `WHERE node_type='concept'
-- GROUP BY node_id, ... COUNT(DISTINCT artifact_id)`. Without it, that
-- query is an unindexed scan + sort over the whole table — measured at
-- 10+ minutes and still not finished on a 1.27M-row table.
CREATE INDEX IF NOT EXISTS idx_artifact_terms_node_lookup
  ON artifact_terms(node_type, node_id, artifact_id);
CREATE TABLE IF NOT EXISTS sync_events (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  details_json TEXT,
  error_json TEXT
);
CREATE TABLE IF NOT EXISTS source_checkpoints (
  source_id TEXT PRIMARY KEY,
  connector_type TEXT NOT NULL,
  cursor_json TEXT NOT NULL,
  high_watermark_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  FOREIGN KEY (source_id) REFERENCES sources(id)
);
CREATE TABLE IF NOT EXISTS source_audit_events (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  action TEXT NOT NULL,
  actor TEXT,
  transport TEXT,
  client_id TEXT,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
  source_name TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idp_groups (
  principal TEXT NOT NULL,
  group_name TEXT NOT NULL,
  PRIMARY KEY (principal, group_name)
);
CREATE TABLE IF NOT EXISTS idp_sync_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Step 33.5 — the structured face of the agent-memory records that already
-- live as Markdown files under the `type: memory` source.
--
-- This is a **projection**, not a second source of truth: every column is
-- derivable from the record files, and `replace_memory_records` rebuilds a
-- source's rows wholesale on each sync, exactly as `chunks_fts` is a derived
-- cache over `chunks` + `artifacts`. Losing this table costs a re-sync, never
-- data. The records themselves stay append-only files on disk.
--
-- It exists because scope/subject/asserted_at/supersedes were reachable only
-- as *prose inside the indexed chunk text* — so nothing could filter on them,
-- and a superseded record stayed retrievable until a batch job archived it.
--
-- `valid_until` is derived, never double-stored: when B supersedes A, A's
-- validity ends at B's `asserted_at`. An explicit `valid_until` in the record
-- wins when it is earlier.
CREATE TABLE IF NOT EXISTS memory_records (
  record_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  subject TEXT,
  kind TEXT NOT NULL DEFAULT 'fact',
  asserted_at TEXT NOT NULL,
  valid_from TEXT,
  valid_until TEXT,
  supersedes TEXT,
  tags TEXT,
  written_by TEXT,
  salience REAL NOT NULL DEFAULT 1.0,
  uses INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  -- Phase 1 (agent-speed memory compaction): `canon_key` is a pure function
  -- of the record's own fields (see pheasant.memory.normalize), so it is
  -- recomputed on every projection rebuild like every other column above
  -- this line, never carried over. `observations`/`last_seen`/`variants`
  -- are earned by reinforcement (a near-duplicate write folding into this
  -- record instead of creating its own file) and are carried over on
  -- rebuild exactly like `salience`/`uses`/`last_used_at` below.
  canon_key TEXT,
  observations INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT,
  variants TEXT,
  -- Phase 3: `tier` and `subsumed_by` are earned by a compaction pass (a
  -- near-duplicate *cluster*, as opposed to Phase 1's exact canonical-key
  -- match) choosing a medoid and demoting the rest — carried over on
  -- rebuild exactly like the Phase 1 columns above. Deliberately DISTINCT
  -- from `supersedes`/`valid_until`: a subsumed record is redundant but
  -- still TRUE, so `subsumed_by` must never feed `effective_valid_until`
  -- (memory/projection.py) — conflating the two would silently expire
  -- facts that are still valid. `tier` is `hot` (default, in every result
  -- set a policy would normally return) or `cold` (demoted; excluded from
  -- default results, reachable via an explicit tier filter or
  -- `current_only=False`/`as_of`, same as a retained superseded record).
  tier TEXT NOT NULL DEFAULT 'hot',
  subsumed_by TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1
  -- Deliberately NO `FOREIGN KEY (artifact_id) REFERENCES artifacts(id)`
  -- here (there was one before this comment; removing it fixed a real,
  -- reproduced-against-a-real-Postgres bug — CLAUDE.md rule 10). SQLite
  -- never enforced it (no `PRAGMA foreign_keys=ON` exists anywhere in this
  -- codebase), but a real Postgres connection enforces every declared FK by
  -- default, and `delete_source_artifacts`/`delete_artifacts` *deliberately*
  -- delete an `artifacts` row while leaving its `memory_records` row alone
  -- (see those methods' own docstrings: wiping earned `uses`/`salience`/
  -- `observations`/`tier` on every consolidation pass would reset a
  -- memory's track record for no benefit, since `replace_memory_records`
  -- rebuilds the row moments later regardless). Under Postgres with the FK
  -- declared, that DELETE raises `foreign key constraint ... still
  -- referenced from table "memory_records"` and aborts the whole
  -- transaction — every full sync of any source once a single memory
  -- record existed, and after Phase 0 (agent-speed memory compaction) also
  -- `_drop_archived`'s targeted `delete_artifacts` on every consolidation
  -- pass that archived anything. Same reasoning `memory_compactions`
  -- already documents for its own `member_id`/`canonical_id` columns —
  -- applied here to the column that predates this plan.
);
-- The `idx_memory_records_canon_key` (Phase 1) and `idx_memory_records_tier`
-- (Phase 3) indexes are NOT declared here: on a fresh database this CREATE
-- TABLE already carries both columns, so they could be, but on an upgraded
-- one the columns are added later by guarded ALTER TABLE in
-- StateStore.migrate() — and `executescript` runs this whole file as one
-- script, before those ALTERs ever run. Declaring an index on a column that
-- does not exist yet would fail against a pre-Phase-1/3 table. See migrate().
-- Retrieval joins chunks -> artifacts -> memory_records on every memory-aware
-- query, and the validity predicate is `scope` + `valid_until`.
CREATE INDEX IF NOT EXISTS idx_memory_records_artifact_id
  ON memory_records(artifact_id);
CREATE INDEX IF NOT EXISTS idx_memory_records_scope
  ON memory_records(scope, valid_until);
-- Append-only audit trail for every compaction decision (Phase 3): a
-- near-duplicate cluster's medoid promotion, one row per subsumed member.
-- `op` is currently always `subsume`, kept as a column rather than a fixed
-- value so a later op (e.g. an LLM-synthesized merge, Phase 4) needs no
-- schema change. `id` is a deterministic hash of
-- (op, member_id, canonical_id, params_hash), so re-running a pass over
-- unchanged content with unchanged parameters writes the SAME row id and
-- `INSERT OR IGNORE` makes the pass idempotent — the property
-- `MemoryStore.consolidate` already has for archiving.
-- `member_id`/`canonical_id` are `memory_records.record_id`, not enforced as
-- a live FK (SQLite never enforces FKs here regardless — see CLAUDE.md's
-- note on `delete_source_artifacts`, and a member's canonical record could,
-- in principle, itself later be superseded or subsumed by something else,
-- at which point the ledger row is history rather than a pointer that must
-- still resolve).
CREATE TABLE IF NOT EXISTS memory_compactions (
  id TEXT PRIMARY KEY,
  op TEXT NOT NULL,
  member_id TEXT NOT NULL,
  canonical_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_compactions_canonical
  ON memory_compactions(canonical_id);
CREATE INDEX IF NOT EXISTS idx_memory_compactions_member
  ON memory_compactions(member_id);
-- What the indexed state was built with, per scope (a source, or the vector
-- space). A restart compares the live config against these: same fingerprint
-- means the stored artifacts/chunks/vectors are still valid and there is
-- nothing to redo. See pheasant.sync.fingerprint.
-- Phase 35.4: per-source write leases. EngineLease permits one writer per
-- /state dir, which is the right model for SQLite and is the ceiling Phase 35
-- lifts: two different sources have no reason to wait for each other. The row
-- is claimed by a single conditional UPDATE, so the database arbitrates races
-- rather than a read-then-write in Python.
CREATE TABLE IF NOT EXISTS source_leases (
  source_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_fingerprints (
  scope TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- Phase 35.5: the durable index work queue. Without it, `sync_all` holds its
-- remaining sources in a Python list: a process killed nine sources into ten
-- has lost the tenth, and nothing outside that process can see the backlog
-- or act on it. A row survives the process, so a restart resumes and a
-- scheduler has a queue depth to scale on.
--
-- `visible_at` is the visibility timeout: a claimed task is invisible until
-- it expires, so a worker that dies mid-task releases it by simply not
-- heartbeating. At-least-once redelivery is safe here because indexing is
-- already idempotent by design (content sha256 + stable IDs) — the existing
-- pillar is what makes the queue cheap.
CREATE TABLE IF NOT EXISTS index_tasks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  payload TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  owner TEXT,
  visible_at TEXT NOT NULL,
  enqueued_at TEXT NOT NULL,
  updated_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_tasks_claim
  ON index_tasks(status, visible_at, enqueued_at);
-- The log tier's own queue, deliberately NOT a `kind` column on index_tasks.
-- Observations arrive at request rate against a corpus that changes hourly at
-- most, so sharing a table would mean request-rate churn on the very index
-- (idx_index_tasks_claim) the indexer claims from, plus the vacuum pressure
-- that comes with it under Postgres — exactly the burden the separate tier
-- exists to avoid. The cost of separating is small because the abstraction was
-- already right: `drain()` is task-agnostic and is reused verbatim, and the
-- race-free conditional-UPDATE claim stays one implementation parameterized by
-- table name.
--
-- No `source_id`/`mode`: those are indexing vocabulary. A batch is opaque JSON
-- and the payload is the whole task.
CREATE TABLE IF NOT EXISTS log_tasks (
  id TEXT PRIMARY KEY,
  payload TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  owner TEXT,
  visible_at TEXT NOT NULL,
  enqueued_at TEXT NOT NULL,
  updated_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_tasks_claim
  ON log_tasks(status, visible_at, enqueued_at);
-- The observation plane: one row per API/MCP call, when
-- `observability.interactions.enabled`.
--
-- **These are not memory records and must never become them.** A row here is
-- never a file, never chunked, never indexed, and never returned by a search;
-- a UI session's chat does not become knowledge because it was observed. The
-- only path from here into memory is a *candidate* that something admits,
-- and admission goes through MemoryStore.append like every other write. See
-- docs/memory-formation.md.
--
-- Unlike every other table in this file, this one is high-churn and
-- retention-bounded: rows are deleted once past
-- `interactions.hot_retention_days`, after being rolled to Parquet under
-- /exports when `cold_enabled`. That is the one sanctioned exception to
-- "nothing is ever deleted" (CLAUDE.md rule 2) and it is why the retention is
-- a declared, documented policy rather than an implementation detail.
--
-- `id` is blake2b(trace_id|span_id), so at-least-once redelivery of a batch
-- is a no-op rather than a duplicate — the same argument index_tasks makes
-- from content sha256, reached a different way.
CREATE TABLE IF NOT EXISTS interaction_events (
  id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_span_id TEXT,
  modality TEXT NOT NULL,
  operation TEXT NOT NULL,
  principal TEXT,
  session_id TEXT,
  client_id TEXT,
  started_at TEXT NOT NULL,
  duration_ms REAL,
  status TEXT NOT NULL,
  query_text TEXT,
  answer_text TEXT,
  criteria_json TEXT,
  -- Stable node ids and source-relative paths, kept in two homogeneous lists
  -- rather than one heterogeneous one. `result_ids` joins to graph_nodes and
  -- chunks; `result_paths` is in the grammar steering rules already match
  -- against, so a `preference` rule minted from these can actually fire.
  result_ids_json TEXT,
  result_paths_json TEXT,
  result_count INTEGER,
  top_score REAL,
  attributes_json TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_interaction_events_time
  ON interaction_events(started_at);
CREATE INDEX IF NOT EXISTS idx_interaction_events_session
  ON interaction_events(session_id, started_at);
"""

#: SQLite-only: WAL, plus the FTS5 virtual tables.
SQLITE_EXTRAS = """\
PRAGMA journal_mode=WAL;
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  source_id UNINDEXED,
  artifact_id UNINDEXED,
  title,
  path,
  heading_path,
  text
);
-- The corpus's own vocabulary, with document frequencies, read straight off
-- the FTS index. `fts5vocab` is a view over chunks_fts's internal term table:
-- it stores nothing of its own and stays exact as the index changes.
--
-- This replaces the concept layer as the source of "what is this corpus
-- about" (the Synapse contract's vocabulary.top_concepts + minhash, and the
-- planner's structural grounding). Concept extraction had been materializing
-- 141k nodes and 1.27M artifact_terms rows to answer that question; SQLite
-- was already maintaining the same information for free.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab(chunks_fts, 'row');
"""

#: Postgres-only: ``chunks_fts`` as a real table with the same columns, so the
#: write path is untouched, plus the generated search vector and its index.
#:
#: ``search_vector`` is a STORED generated column rather than a trigger: it
#: cannot drift from its row, and there is no ordering hazard between the
#: INSERT and a trigger that a concurrent reader could observe.
#:
#: **Punctuation is flattened to spaces before tokenizing**, which is not
#: cosmetic. SQLite indexes these columns with FTS5's ``unicode61`` tokenizer,
#: which splits on every non-alphanumeric: ``deploy-gateway.md`` becomes
#: ``deploy``, ``gateway``, ``md``. Postgres's ``simple`` dictionary keeps it
#: as the single lexeme ``deploy-gateway.md``, so a search for "deploy" did
#: not match the file *named* for it at all — silently, with no error and a
#: perfectly plausible result list. Measured: the file named for the query
#: ranked below a decoy that merely repeats it in prose. The regexp restores
#: unicode61's splitting so both backends see the same terms.
POSTGRES_EXTRAS = """\
CREATE TABLE IF NOT EXISTS chunks_fts (
  chunk_id TEXT PRIMARY KEY,
  source_id TEXT,
  artifact_id TEXT,
  title TEXT,
  path TEXT,
  heading_path TEXT,
  text TEXT,
  search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(title, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'A') ||
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(path, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'B') ||
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(heading_path, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'C') ||
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(text, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'D')
  ) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_vector ON chunks_fts USING GIN (search_vector);
-- Mirrors the per-artifact and per-source DELETEs the write path issues.
CREATE INDEX IF NOT EXISTS idx_chunks_fts_artifact ON chunks_fts(artifact_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_source ON chunks_fts(source_id);
"""


def schema_for(dialect: Dialect) -> str:
    """The full DDL script for one dialect.

    Type substitution is applied longest-key-first so ``INTEGER PRIMARY KEY``
    is never half-rewritten into ``BIGINT PRIMARY KEY PRIMARY KEY``.
    """

    body = CORE_SCHEMA
    for source, target in sorted(dialect.types.items(), key=lambda kv: -len(kv[0])):
        body = body.replace(source, target)
    extras = POSTGRES_EXTRAS if dialect.is_postgres else SQLITE_EXTRAS
    return body + extras


#: One ``CREATE TABLE IF NOT EXISTS <name> ( … );`` block in :data:`CORE_SCHEMA`.
#: Every block in that string ends on its own ``);`` line, which is what makes
#: the non-greedy body match unambiguous.
_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", re.DOTALL | re.IGNORECASE
)

#: A ``--`` comment, stripped before a table body is split on commas so a
#: comma inside prose cannot look like a column boundary.
_SQL_COMMENT = re.compile(r"--[^\n]*")

#: Table-level constraints, which are entries in the comma-separated body but
#: are not columns.
_CONSTRAINT_KEYWORDS = frozenset({"FOREIGN", "PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT"})


def _split_top_level(body: str) -> list[str]:
    """Split a table body on commas that are not inside parentheses.

    ``PRIMARY KEY (principal, group_name)`` is one entry, not two — and a
    naive ``body.split(",")`` would silently invent a ``group_name)`` column.
    """

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    parts.append("".join(current))
    return parts


def columns_of(table: str) -> list[tuple[str, str]]:
    """``[(column, portable type)]`` for a core table, in declaration order.

    Read off :data:`CORE_SCHEMA` rather than from a live database, for two
    reasons. Order: ``StateBackend.table_columns`` answers with a *set*,
    which is the right shape for the additive migrations that ask it "does
    this column exist yet" and the wrong shape for anything that has to
    produce a stable column layout. Availability: a caller can ask what a
    table looks like without opening a connection at all.

    The type is the portable spelling (``TEXT`` / ``INTEGER`` / ``REAL``),
    before :func:`schema_for` substitutes the dialect's own. Returns an empty
    list for a table that is not in the shared schema — ``chunks_fts`` and
    ``chunks_vocab`` live in the per-dialect extras and genuinely have no one
    portable definition.
    """

    for name, body in _CREATE_TABLE.findall(CORE_SCHEMA):
        if name.lower() != table.lower():
            continue
        columns: list[tuple[str, str]] = []
        for entry in _split_top_level(_SQL_COMMENT.sub("", body)):
            tokens = entry.split()
            if not tokens or tokens[0].upper() in _CONSTRAINT_KEYWORDS:
                continue
            columns.append((tokens[0], tokens[1].upper() if len(tokens) > 1 else "TEXT"))
        return columns
    return []


def primary_key_of(table: str) -> str | None:
    """The single-column primary key of a core table, or ``None``.

    ``None`` covers both "no primary key" and a *composite* one
    (``idp_groups``), because the caller this exists for — keyset pagination
    in :mod:`pheasant.analytics` — needs one orderable column and a composite
    key is not that.
    """

    for name, body in _CREATE_TABLE.findall(CORE_SCHEMA):
        if name.lower() != table.lower():
            continue
        for entry in _split_top_level(_SQL_COMMENT.sub("", body)):
            tokens = entry.split()
            if not tokens or tokens[0].upper() in _CONSTRAINT_KEYWORDS:
                continue
            if "PRIMARY KEY" in " ".join(tokens[1:]).upper():
                return tokens[0]
        return None
    return None
