from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
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
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  source_id UNINDEXED,
  artifact_id UNINDEXED,
  title,
  path,
  heading_path,
  text
);
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
"""


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # Crash/concurrency safety (Synapse step 21.2): WAL survives
            # kill -9 mid-write, busy_timeout rides out concurrent readers,
            # synchronous=NORMAL is the sanctioned WAL durability level.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        # Step 32.1 — one-shot idempotent column add (additive; existing rows
        # keep acl NULL = "source expressed no ACL", the pre-32 semantics).
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(artifacts)")}
        if "acl" not in columns:
            self.conn.execute("ALTER TABLE artifacts ADD COLUMN acl TEXT")
        self.conn.commit()

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
    ) -> None:
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
            self.conn.execute("DELETE FROM chunks WHERE artifact_id=?", (artifact["id"],))
            self.conn.execute("DELETE FROM chunks_fts WHERE artifact_id=?", (artifact["id"],))
            self.conn.execute("DELETE FROM symbols WHERE artifact_id=?", (artifact["id"],))
            self.conn.execute("DELETE FROM artifact_terms WHERE artifact_id=?", (artifact["id"],))
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
                        artifact["relative_path"] or artifact["path"],
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

    def delete_source_artifacts(self, source_id: str) -> int:
        rows = self.rows("SELECT COUNT(*) AS c FROM artifacts WHERE source_id=?", (source_id,))
        count = int(rows[0]["c"]) if rows else 0
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM symbols WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifacts WHERE source_id=?", (source_id,))
        return count

    def delete_source(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM symbols WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE source_id=?", (source_id,))
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

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
