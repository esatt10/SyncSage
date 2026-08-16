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

from pheasant.persistence.sql import Dialect

#: Tables and indexes, shared by every backend.
CORE_SCHEMA = """\
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
-- Supports GraphBuilder.reconcile_concepts: `WHERE node_type='concept'
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
  schema_version INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
-- Retrieval joins chunks -> artifacts -> memory_records on every memory-aware
-- query, and the validity predicate is `scope` + `valid_until`.
CREATE INDEX IF NOT EXISTS idx_memory_records_artifact_id
  ON memory_records(artifact_id);
CREATE INDEX IF NOT EXISTS idx_memory_records_scope
  ON memory_records(scope, valid_until);
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
