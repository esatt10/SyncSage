from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pheasant.persistence.backends import SqliteBackend, StateBackend, open_backend
from pheasant.persistence.schema import schema_for
from pheasant.persistence.secrets import resolve_dsn
from pheasant.persistence.sql import SQLITE

logger = logging.getLogger(__name__)

#: How long a statement waits for a competing writer before giving up.
#:
#: Was 5 s, which is fine when the only writer is a short sync. It is **not**
#: fine during a multi-hour index of a large repository with the UI open: the
#: sync worker writes continuously in one process while the API server serves
#: `/overview`, `/jobs` and `/graph/slice` polls in another, and a 12,667-file
#: run of microsoft/vscode died on
#:
#:     sqlite3.OperationalError: database is locked
#:
#: after ~40 minutes. WAL lets readers and one writer coexist, but checkpoints
#: and the enrichment pass's multi-statement transactions still take the write
#: lock long enough to blow a 5-second budget under that load. Waiting a minute
#: costs nothing when the alternative is losing the whole index.
BUSY_TIMEOUT_MS = 60_000


def _basename(path: str | None) -> str:
    """Final path segment, for the FTS ``title`` column.

    Deliberately not ``Path(...).name``: these are POSIX-style relative paths
    recorded at index time, and on Windows ``PurePath`` would also split on
    backslashes, so an indexed path containing one would produce a different
    title than the same corpus indexed on Linux.
    """
    text = str(path or "")
    return text.rsplit("/", 1)[-1] if "/" in text else text


#: Re-exported for callers that referenced ``state_store.SCHEMA``; the
#: definitions now live in :mod:`pheasant.persistence.schema`, per dialect.
SCHEMA = schema_for(SQLITE)


class StateStore:
    """SQLite-backed state, one connection per thread.

    WAL mode (set below) is SQLite's sanctioned way to let multiple threads
    read the same database truly concurrently — but only when each thread
    uses its *own* connection/cursor. An earlier version of this class shared
    one `sqlite3.Connection` across every thread: the search paths
    (text/vector/graph) run in parallel across threads (hybrid.py's per-mode
    pool, nested inside multi_search's per-query pool for the agentic
    workflow), and overlapping execute()+fetch calls on one shared connection
    interleaved cursor state, handing back a Row missing an expected column
    (reproduced as `row["chunk_id"]` raising IndexError from
    vector_store.search() under concurrent load). The fix at the time was a
    lock serializing every read through `rows()` — correct, but it throws
    away the concurrency WAL mode exists to provide: with four queries
    fanning out across three search modes, a global lock turned an
    embarrassingly-parallel retrieve step into a mostly-serial one (measured:
    the first, multi-query retrieve of an agentic run took 22-29s; a later
    single-query retrieve over the same data took 1.3-2s).

    Thread-local connections remove the shared state instead of locking
    around it: every thread gets its own connection and cursor, so there is
    nothing left to interleave, and reads run genuinely in parallel again.
    Writes still only ever happen from the sync worker process (a different
    process entirely — see sync/worker.py), so this process's connections are
    all readers as far as WAL's MVCC snapshot isolation is concerned.
    """

    def __init__(self, path: str | Path | None = None, *, backend: StateBackend | None = None):
        """Open the store.

        ``path`` is what every existing caller passes and still means "a
        SQLite file here". ``backend`` is the Phase-35.2 seam: pass a
        :class:`~pheasant.persistence.backends.StateBackend` (see
        :func:`from_config`) to run on Postgres instead. Exactly one is
        required.
        """

        if backend is None:
            if path is None:
                raise ValueError("StateStore needs either a path or a backend")
            backend = SqliteBackend(path)
        self.backend = backend
        # Kept for the many callers that read `store.path` (backups, logging,
        # the migration command). None on a backend that has no file.
        self.path = getattr(backend, "path", None)

    @classmethod
    def from_config(cls, config: Any, path: str | Path | None = None) -> StateStore:
        """Build the store the config asks for.

        Falls back to ``path`` for the SQLite default so a caller can keep
        passing the location it already computed from :class:`StatePaths`.
        """

        storage = getattr(config, "storage", None)
        backend_name = str(getattr(storage, "backend", "sqlite") or "sqlite")
        if backend_name.lower() != "postgres":
            return cls(path)
        return cls(
            backend=open_backend(
                path,
                backend="postgres",
                dsn=resolve_dsn(storage),
                pool_size=int(getattr(storage, "pool_size", 10) or 10),
            )
        )

    @property
    def dialect(self):
        return self.backend.dialect

    @property
    def conn(self):
        """The live connection.

        On SQLite this is the real ``sqlite3.Connection``, unchanged. On
        Postgres it is a connection-shaped adapter, so the 76 ``self.conn``
        uses below — including ten ``with self.conn:`` transaction blocks —
        work on both without being rewritten. Rewriting them would have put 76
        freshly edited lines on the default path to gain nothing.
        """

        return self.backend.conn

    def migrate(self) -> None:
        self.backend.executescript(schema_for(self.backend.dialect))
        # Step 32.1 — one-shot idempotent column add (additive; existing rows
        # keep acl NULL = "source expressed no ACL", the pre-32 semantics).
        if "acl" not in self.backend.table_columns("artifacts"):
            self.conn.execute("ALTER TABLE artifacts ADD COLUMN acl TEXT")
        self.conn.commit()
        self._migrate_fts_titles()

    def _migrate_fts_titles(self) -> None:
        """Re-point ``chunks_fts.title`` at the file's basename.

        It used to hold the full relative path — the identical string already
        in ``path`` — so a filename carried no signal BM25 could weight
        separately, and searching "readme" ranked by body brevity instead of
        by name. Rebuilding is safe and needs no re-index: ``chunks_fts`` is a
        derived cache over ``chunks`` + ``artifacts``, exactly like the graph
        node index, so it can be regenerated from the tables that are the
        truth. No user data is touched.

        One-shot and idempotent: the presence of a '/' in any stored title is
        the old format, and after the rebuild there is nothing left to detect.
        """
        if self.backend.dialect.fulltext != "fts5":
            # Postgres derives its search vector from `chunks` directly, so
            # there is no separate FTS table whose titles could be stale.
            return
        try:
            stale = self.conn.execute(
                "SELECT 1 FROM chunks_fts WHERE title LIKE '%/%' LIMIT 1"
            ).fetchone()
        except sqlite3.Error:  # table absent or FTS5 unavailable — nothing to do
            return
        if stale is None:
            return
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts")
            self.conn.execute(
                """INSERT INTO chunks_fts(
                       chunk_id, source_id, artifact_id, title, path, heading_path, text)
                   SELECT chunks.id, chunks.source_id, chunks.artifact_id,
                          replace(
                              COALESCE(artifacts.relative_path, artifacts.path),
                              rtrim(COALESCE(artifacts.relative_path, artifacts.path),
                                    replace(COALESCE(artifacts.relative_path, artifacts.path),
                                            '/', '')),
                              ''),
                          COALESCE(artifacts.relative_path, artifacts.path),
                          COALESCE(chunks.heading_path, ''),
                          chunks.text
                   FROM chunks JOIN artifacts ON artifacts.id = chunks.artifact_id"""
            )

    def get_fingerprint(self, scope: str) -> str | None:
        """What this scope was last indexed with, or None if never recorded."""

        rows = self.rows("SELECT fingerprint FROM sync_fingerprints WHERE scope=?", (scope,))
        return str(rows[0]["fingerprint"]) if rows else None

    def set_fingerprint(self, scope: str, fingerprint: str, updated_at: str) -> None:
        """Record a scope's fingerprint after a successful pass over it."""

        with self.conn:
            self.conn.execute(
                "INSERT INTO sync_fingerprints (scope, fingerprint, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "updated_at=excluded.updated_at",
                (scope, fingerprint, updated_at),
            )

    def clear_fingerprint(self, scope: str) -> None:
        """Drop a scope's fingerprint row. A no-op if it was never set."""

        with self.conn:
            self.conn.execute("DELETE FROM sync_fingerprints WHERE scope=?", (scope,))

    def replace_idp_groups(self, mapping: dict[str, list[str]], synced_at: str) -> bool:
        """Persist one IdP sync pass (Step 32.4). Returns True when rows changed.

        The mapping replace is transactional; ``synced_at`` bumps on every
        call (an unchanged directory still counts as a fresh heartbeat —
        that heartbeat is the staleness-SLA clock).
        """
        desired = {(principal, group) for principal, groups in mapping.items() for group in groups}
        current = {
            (row[0], row[1])
            for row in self.conn.execute("SELECT principal, group_name FROM idp_groups")
        }
        changed = desired != current
        with self.conn:
            if changed:
                self.conn.execute("DELETE FROM idp_groups")
                self.conn.executemany(
                    "INSERT INTO idp_groups(principal, group_name) VALUES(?,?)",
                    sorted(desired),
                )
            self.conn.execute(
                """INSERT INTO idp_sync_meta(key, value) VALUES('synced_at', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (synced_at,),
            )
        return changed

    def idp_groups_for(self, principals: set[str]) -> set[str]:
        """Group names the IdP mapping grants any of these principal spellings."""
        if not principals:
            return set()
        ordered = sorted(principals)
        placeholders = ",".join("?" for _ in ordered)
        rows = self.conn.execute(
            f"SELECT group_name FROM idp_groups WHERE principal IN ({placeholders})",
            tuple(ordered),
        )
        return {row[0] for row in rows}

    def idp_synced_at(self) -> str | None:
        rows = self.rows("SELECT value FROM idp_sync_meta WHERE key='synced_at'")
        return str(rows[0]["value"]) if rows else None

    def idp_principal_count(self) -> int:
        rows = self.rows("SELECT COUNT(DISTINCT principal) AS c FROM idp_groups")
        return int(rows[0]["c"]) if rows else 0

    def artifact_acls(self, artifact_ids: list[str]) -> dict[str, str | None]:
        """The stored ACL JSON (or None) for each artifact id (Step 32.2)."""
        if not artifact_ids:
            return {}
        placeholders = ",".join("?" for _ in artifact_ids)
        rows = self.conn.execute(
            f"SELECT id, acl FROM artifacts WHERE id IN ({placeholders})",
            tuple(artifact_ids),
        )
        return {row[0]: row[1] for row in rows}

    def upsert_knowledge_base(
        self,
        kb_id: str,
        name: str,
        description: str | None,
        cfg_hash: str,
        now: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO knowledge_bases(
                id,name,description,config_hash,created_at,updated_at
            )
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                config_hash=excluded.config_hash,
                updated_at=excluded.updated_at""",
            (kb_id, name, description, cfg_hash, now, now),
        )
        self.conn.commit()

    def upsert_source(
        self,
        source_id: str,
        kb_id: str,
        name: str,
        source_type: str,
        path: str,
        enabled: bool,
        config: dict[str, Any],
        status: str = "registered",
    ) -> None:
        self.conn.execute(
            """INSERT INTO sources(
                id,knowledge_base_id,name,type,path,enabled,config_json,last_status
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                type=excluded.type,
                path=excluded.path,
                enabled=excluded.enabled,
                config_json=excluded.config_json,
                last_status=COALESCE(sources.last_status, excluded.last_status)""",
            (
                source_id,
                kb_id,
                name,
                source_type,
                path,
                int(enabled),
                json.dumps(config, default=str),
                status,
            ),
        )
        self.conn.commit()

    def replace_artifact_chunks(
        self,
        artifact: dict[str, Any],
        chunks: list[dict[str, Any]],
        *,
        fresh: bool = False,
    ) -> None:
        """Write one artifact and its chunks, replacing whatever was there.

        ``fresh`` says the caller has already cleared this source, so there is
        nothing to replace. It exists because of a measured O(N²), found by
        the Phase 35.7 capacity sweep: indexing 500 → 8,000 files took 1.9s →
        164.7s, tripling for every doubling.

        The cause is that ``chunks_fts.artifact_id`` is an **UNINDEXED** FTS5
        column, so ``DELETE FROM chunks_fts WHERE artifact_id=?`` is a full
        scan of a table that grows with the corpus — once per artifact. On a
        *full* sync `delete_source_artifacts` has already emptied the source's
        rows before the loop starts, so each of those scans searches an
        ever-larger table in order to delete **nothing**.

        Same class as the ``graph_nodes_fts`` scan recorded in CLAUDE.md, and
        the same fix: do not ask an unindexed column a question whose answer
        is already known.

        **SQLite only.** On Postgres ``chunks_fts`` is an ordinary table with
        ``idx_chunks_fts_artifact``, so the delete was always indexed — the
        bug is a property of FTS5's UNINDEXED columns, not of the query.

        A sweep of every remaining FTS5 write (2026-08-16) found no other
        unmitigated instance: ``delete_source_artifacts`` scans on
        ``source_id`` but runs **once per sync** rather than once per
        artifact and has to visit those rows to delete them anyway, and
        ``graph_nodes_fts``'s batched delete is bounded by
        ``SyncEngine._INDEX_REBUILD_THRESHOLD``, above which it rebuilds
        wholesale instead.
        """

        with self.conn:
            self.conn.execute(
                """INSERT INTO artifacts(
                    id,source_id,type,path,relative_path,mime_type,size_bytes,
                    sha256,mtime,git_branch,git_commit,last_indexed_at,status,acl
                )
                VALUES(
                    :id,:source_id,:type,:path,:relative_path,:mime_type,:size_bytes,
                    :sha256,:mtime,:git_branch,:git_commit,:last_indexed_at,:status,:acl
                )
                ON CONFLICT(id) DO UPDATE SET
                    type=excluded.type,
                    path=excluded.path,
                    relative_path=excluded.relative_path,
                    mime_type=excluded.mime_type,
                    size_bytes=excluded.size_bytes,
                    sha256=excluded.sha256,
                    mtime=excluded.mtime,
                    git_branch=excluded.git_branch,
                    git_commit=excluded.git_commit,
                    last_indexed_at=excluded.last_indexed_at,
                    status=excluded.status,
                    acl=excluded.acl""",
                {"acl": None, **artifact},
            )
            if not fresh:
                self.conn.execute("DELETE FROM chunks WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute("DELETE FROM chunks_fts WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute("DELETE FROM symbols WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute(
                    "DELETE FROM artifact_terms WHERE artifact_id=?", (artifact["id"],)
                )
            for chunk in chunks:
                self.conn.execute(
                    """INSERT INTO chunks(
                        id,artifact_id,source_id,chunk_index,heading_path,start_line,
                        end_line,text,text_hash,summary,token_estimate
                    )
                    VALUES(
                        :id,:artifact_id,:source_id,:chunk_index,:heading_path,
                        :start_line,:end_line,:text,:text_hash,:summary,:token_estimate
                    )""",
                    chunk,
                )
                self.conn.execute(
                    """INSERT INTO chunks_fts(
                        chunk_id,source_id,artifact_id,title,path,heading_path,text
                    )
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        chunk["id"],
                        chunk["source_id"],
                        chunk["artifact_id"],
                        # title = basename, path = full relative path. Two
                        # distinct signals so BM25 can weight a filename match
                        # above a body match (see sqlite_store._BM25_WEIGHTS);
                        # storing the same string twice made them one.
                        _basename(artifact["relative_path"] or artifact["path"]),
                        artifact["relative_path"] or artifact["path"],
                        chunk.get("heading_path") or "",
                        chunk["text"],
                    ),
                )

    def replace_artifact_enrichment(
        self,
        artifact_id: str,
        source_id: str,
        terms: list[dict[str, Any]],
        symbols: list[dict[str, Any]],
    ) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM symbols WHERE artifact_id=?", (artifact_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE artifact_id=?", (artifact_id,))
            seen_terms: set[tuple[str, str, str]] = set()
            for index, term in enumerate(terms):
                key = (
                    term["node_id"],
                    term["node_type"],
                    term["normalized_term"],
                )
                if key in seen_terms:
                    continue
                seen_terms.add(key)
                self.conn.execute(
                    """INSERT INTO artifact_terms(
                        id,artifact_id,source_id,node_id,node_type,term,
                        normalized_term,weight,metadata_json
                    )
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{artifact_id}:term:{index:04d}",
                        artifact_id,
                        source_id,
                        term["node_id"],
                        term["node_type"],
                        term["term"],
                        term["normalized_term"],
                        float(term.get("weight") or 1.0),
                        json.dumps(term.get("metadata") or {}, default=str),
                    ),
                )
            for symbol in symbols:
                self.conn.execute(
                    """INSERT INTO symbols(
                        id,artifact_id,source_id,language,symbol_type,name,
                        qualified_name,start_line,end_line,signature,docstring_summary
                    )
                    VALUES(
                        :id,:artifact_id,:source_id,:language,:symbol_type,:name,
                        :qualified_name,:start_line,:end_line,:signature,:docstring_summary
                    )""",
                    symbol,
                )

    def mark_source_indexed(self, source_id: str, now: str, status: str = "healthy") -> None:
        self.conn.execute(
            "UPDATE sources SET last_indexed_at=?, last_status=? WHERE id=?",
            (now, status, source_id),
        )
        self.conn.commit()

    def mark_source_status(self, source_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sources SET last_status=? WHERE id=?",
            (status, source_id),
        )
        self.conn.commit()

    def set_source_enabled(self, source_id: str, enabled: bool, status: str | None = None) -> None:
        if status is None:
            self.conn.execute(
                "UPDATE sources SET enabled=? WHERE id=?",
                (int(enabled), source_id),
            )
        else:
            self.conn.execute(
                "UPDATE sources SET enabled=?, last_status=? WHERE id=?",
                (int(enabled), status, source_id),
            )
        self.conn.commit()

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            "SELECT * FROM sources WHERE id=? OR name=? LIMIT 1",
            (source_id, source_id),
        )
        return dict(rows[0]) if rows else None

    def get_source_checkpoint(self, source_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            """SELECT source_id, connector_type, cursor_json, high_watermark_json,
                      updated_at, status
               FROM source_checkpoints WHERE source_id=?""",
            (source_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "source_id": row["source_id"],
            "connector_type": row["connector_type"],
            "cursor": json.loads(row["cursor_json"] or "{}"),
            "high_watermark": json.loads(row["high_watermark_json"] or "{}"),
            "updated_at": row["updated_at"],
            "status": row["status"],
        }

    def set_source_checkpoint(
        self,
        source_id: str,
        connector_type: str,
        cursor: dict[str, Any],
        high_watermark: dict[str, Any],
        updated_at: str,
        status: str = "healthy",
    ) -> None:
        self.conn.execute(
            """INSERT INTO source_checkpoints(
                source_id,connector_type,cursor_json,high_watermark_json,updated_at,status
            )
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                connector_type=excluded.connector_type,
                cursor_json=excluded.cursor_json,
                high_watermark_json=excluded.high_watermark_json,
                updated_at=excluded.updated_at,
                status=excluded.status""",
            (
                source_id,
                connector_type,
                json.dumps(cursor, default=str, sort_keys=True),
                json.dumps(high_watermark, default=str, sort_keys=True),
                updated_at,
                status,
            ),
        )
        self.conn.commit()

    def list_source_checkpoints(self) -> list[dict[str, Any]]:
        rows = self.rows(
            """SELECT source_id, connector_type, cursor_json, high_watermark_json,
                      updated_at, status
               FROM source_checkpoints ORDER BY source_id"""
        )
        return [
            {
                "source_id": row["source_id"],
                "connector_type": row["connector_type"],
                "cursor": json.loads(row["cursor_json"] or "{}"),
                "high_watermark": json.loads(row["high_watermark_json"] or "{}"),
                "updated_at": row["updated_at"],
                "status": row["status"],
            }
            for row in rows
        ]

    def replace_memory_records(self, source_id: str, records: list[dict[str, Any]]) -> int:
        """Rebuild one memory source's projection rows (Step 33.5).

        Transactional and wholesale: the table is a projection of the record
        files, so rebuilding it is always safe and always correct, and a
        record archived on disk disappears here without needing its own
        delete path. Returns the number of rows written.

        ``salience``/``uses``/``last_used_at`` are carried over from the
        existing row when one is present — they are earned by *use* (Step
        33.9) and are the one thing here that is not derivable from the file,
        so a re-sync must not silently reset them to zero.
        """
        with self.conn:
            earned = {
                str(row["record_id"]): row
                for row in self.conn.execute(
                    "SELECT record_id, salience, uses, last_used_at "
                    "FROM memory_records WHERE source_id=?",
                    (source_id,),
                )
            }
            self.conn.execute("DELETE FROM memory_records WHERE source_id=?", (source_id,))
            for record in records:
                prior = earned.get(str(record.get("record_id")))
                self.conn.execute(
                    """INSERT INTO memory_records(
                        record_id,artifact_id,source_id,scope,subject,kind,asserted_at,
                        valid_from,valid_until,supersedes,tags,written_by,
                        salience,uses,last_used_at,schema_version
                    )
                    VALUES(
                        :record_id,:artifact_id,:source_id,:scope,:subject,:kind,:asserted_at,
                        :valid_from,:valid_until,:supersedes,:tags,:written_by,
                        :salience,:uses,:last_used_at,:schema_version
                    )""",
                    {
                        "subject": None,
                        "kind": "fact",
                        "valid_from": None,
                        "valid_until": None,
                        "supersedes": None,
                        "tags": None,
                        "written_by": None,
                        "schema_version": 1,
                        **record,
                        "salience": float(prior["salience"]) if prior else 1.0,
                        "uses": int(prior["uses"]) if prior else 0,
                        "last_used_at": prior["last_used_at"] if prior else None,
                    },
                )
        return len(records)

    def record_memory_use(self, record_ids: list[str], when: str) -> int:
        """Bump `uses`/`last_used_at` for records retrieval just returned.

        Best-effort and off the critical path (Step 33.9): a counter that fails
        to increment costs a little ranking signal, never a search. One
        statement for the whole batch, because this runs on the *read* path and
        a per-record round trip there would be a real cost for a soft benefit.
        """
        if not record_ids:
            return 0
        placeholders = ",".join("?" for _ in record_ids)
        try:
            with self.conn:
                cursor = self.conn.execute(
                    "UPDATE memory_records SET uses = uses + 1, last_used_at = ? "
                    f"WHERE record_id IN ({placeholders})",
                    (when, *record_ids),
                )
            return int(cursor.rowcount or 0)
        except Exception:  # pragma: no cover - never fail a query over a counter
            logger.debug("memory use counters not recorded", exc_info=True)
            return 0

    def memory_salience_rows(self) -> list[dict[str, Any]]:
        """Everything the salience formula reads, for a pruning pass."""
        try:
            rows = self.rows(
                # `kind` is here so capacity pruning can tell a retrieval *rule*
                # from a recallable fact; see `memory.maintenance`.
                "SELECT record_id, scope, kind, asserted_at, uses, last_used_at, salience "
                "FROM memory_records"
            )
        except Exception:  # pragma: no cover - state store older than 33.5
            return []
        return [dict(row) for row in rows]

    def set_memory_salience(self, scores: dict[str, float]) -> None:
        """Persist computed salience so it is visible without recomputing."""
        if not scores:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE memory_records SET salience = ? WHERE record_id = ?",
                [(value, key) for key, value in sorted(scores.items())],
            )

    def delete_source_artifacts(self, source_id: str) -> int:
        rows = self.rows("SELECT COUNT(*) AS c FROM artifacts WHERE source_id=?", (source_id,))
        count = int(rows[0]["c"]) if rows else 0
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM symbols WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifacts WHERE source_id=?", (source_id,))
        # Deliberately NOT clearing memory_records here. This runs on every
        # *full* sync (engine.py's rebuild path, which consolidation takes after
        # each archive), and `replace_memory_records` rebuilds the rows moments
        # later anyway — so dropping them buys nothing and costs the one thing
        # in that table that is earned rather than derived: `uses`, `salience`
        # and `last_used_at`. Wiping those on every consolidation pass would
        # quietly reset a memory's track record. Genuine removal goes through
        # `delete_source`, which does clear them.
        return count

    def delete_artifacts(self, artifact_ids: list[str]) -> int:
        """Remove specific artifacts (and their chunks/symbols/terms) by id.

        Phase 0: the targeted counterpart to `delete_source_artifacts`. That
        method's whole-source `DELETE ... WHERE source_id=?` is cheap because
        every table involved is indexed on `source_id`; this one is a
        per-artifact `WHERE id/artifact_id IN (...)` instead, which is the
        exact shape CLAUDE.md warns against for `chunks_fts` — its
        `artifact_id` column is UNINDEXED, so this degrades to a table scan
        per call. It exists anyway because a handful of archived memory
        records does not justify re-syncing (and re-parsing) an entire
        source; callers own picking a size where a full sync is cheaper
        instead (see `memory.maintenance.MEMORY_TARGETED_ARCHIVE_MAX`).

        Like `delete_source_artifacts`, `memory_records` rows are left alone
        — the caller (memory maintenance) rebuilds that table itself from the
        record files, which is what keeps `uses`/`salience`/`last_used_at`
        earned rather than reset.
        """
        ids = [str(i) for i in artifact_ids if i]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        params = tuple(ids)
        with self.conn:
            self.conn.execute(
                f"DELETE FROM chunks_fts WHERE artifact_id IN ({placeholders})", params
            )
            self.conn.execute(f"DELETE FROM chunks WHERE artifact_id IN ({placeholders})", params)
            self.conn.execute(f"DELETE FROM symbols WHERE artifact_id IN ({placeholders})", params)
            self.conn.execute(
                f"DELETE FROM artifact_terms WHERE artifact_id IN ({placeholders})", params
            )
            cursor = self.conn.execute(
                f"DELETE FROM artifacts WHERE id IN ({placeholders})", params
            )
            return int(cursor.rowcount or 0)

    def delete_source(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM symbols WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM memory_records WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifacts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM source_checkpoints WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def append_sync_event(
        self,
        event_id: str,
        source_id: str | None,
        event_type: str,
        status: str,
        started_at: str | None,
        finished_at: str | None,
        details: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO sync_events(
                id,source_id,event_type,status,started_at,finished_at,details_json,error_json
            )
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_id,
                source_id,
                event_type,
                status,
                started_at,
                finished_at,
                json.dumps(details or {}, default=str, sort_keys=True),
                json.dumps(error, default=str, sort_keys=True) if error else None,
            ),
        )
        self.conn.commit()

    def list_sync_events(
        self,
        source_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Most-recent-first sync events (rowid order is insertion order)."""
        where = ""
        params: list[Any] = []
        if source_id:
            where = "WHERE source_id=?"
            params.append(source_id)
        params.extend([limit, offset])
        rows = self.rows(
            f"""SELECT * FROM sync_events
                {where}
                ORDER BY rowid DESC
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json") or "{}")
            error_json = event.pop("error_json")
            event["error"] = json.loads(error_json) if error_json else None
            events.append(event)
        return events

    def append_source_audit_event(
        self,
        event_id: str,
        source_id: str | None,
        action: str,
        actor: str | None,
        transport: str | None,
        client_id: str | None,
        created_at: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO source_audit_events(
                id,source_id,action,actor,transport,client_id,created_at,details_json
            )
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_id,
                source_id,
                action,
                actor,
                transport,
                client_id,
                created_at,
                json.dumps(details or {}, default=str, sort_keys=True),
            ),
        )
        self.conn.commit()

    def list_source_audit_events(
        self,
        source_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if source_id:
            where = "WHERE source_id=?"
            params.append(source_id)
        params.extend([limit, offset])
        rows = self.rows(
            f"""SELECT * FROM source_audit_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json") or "{}")
            events.append(event)
        return events

    def get_manifest(self, source_name: str) -> dict[str, Any] | None:
        rows = self.rows(
            "SELECT payload_json FROM manifests WHERE source_name=?",
            (source_name,),
        )
        if not rows:
            return None
        return json.loads(rows[0]["payload_json"])

    def set_manifest(self, source_name: str, payload: dict[str, Any], updated_at: str) -> None:
        self.conn.execute(
            """INSERT INTO manifests(source_name,payload_json,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(source_name) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
            (source_name, json.dumps(payload, sort_keys=True, default=str), updated_at),
        )
        self.conn.commit()

    def manifest_exists(self, source_name: str) -> bool:
        rows = self.rows(
            "SELECT 1 FROM manifests WHERE source_name=? LIMIT 1",
            (source_name,),
        )
        return bool(rows)

    def delete_manifest(self, source_name: str) -> None:
        self.conn.execute("DELETE FROM manifests WHERE source_name=?", (source_name,))
        self.conn.commit()

    def artifact_state(self, source_id: str, artifact_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            """SELECT artifacts.id, artifacts.sha256, artifacts.status,
                      COUNT(chunks.id) AS chunk_count
               FROM artifacts
               LEFT JOIN chunks ON chunks.artifact_id=artifacts.id
               WHERE artifacts.source_id=? AND artifacts.id=?
               GROUP BY artifacts.id""",
            (source_id, artifact_id),
        )
        return dict(rows[0]) if rows else None

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        """Run raw SQL. The escape hatch 57 call sites across the codebase use.

        Routed through the backend so the statement is translated for the
        active dialect (``?`` → ``%s``, ``GROUP_CONCAT`` → ``string_agg``).
        On SQLite the translation is a no-op and this executes byte-identical
        SQL over the same connection it always did.
        """

        return self.backend.rows(sql, params)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Run a raw write and commit it (Phase 35.5).

        The write counterpart to :meth:`rows`, and it commits because it is
        used by the durable index queue, whose whole point is that a claim
        or an ack is visible to another process the moment it returns. A
        queue operation batched into someone else's open transaction would
        be a queue that only works inside one process.
        """

        self.backend.execute(sql, params)
        self.backend.commit()

    def execute_returning(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        """Run a write that returns rows (``UPDATE ... RETURNING``) and commit.

        Not a convenience over :meth:`rows`: routing a write through the read
        path leaves the implicit transaction **open**, so on SQLite the WAL
        write lock is held until the connection happens to commit something
        else. Every other thread then blocks for the full ``busy_timeout``,
        and — worse for a queue — the claim is not visible to another process
        at all, which is the one property the durable queue exists to have.
        """

        # `statement`, not `rows`: on a pooled backend a read may hand the
        # connection back, and the commit below would then land on a
        # different connection — silently discarding the UPDATE.
        result = self.backend.statement(sql, params)
        self.backend.commit()
        return result

    def close(self) -> None:
        self.backend.close()
