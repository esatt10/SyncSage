from __future__ import annotations

import json
import sqlite3
import threading
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
"""


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            return self._conn

    def migrate(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def upsert_knowledge_base(self, kb_id: str, name: str, description: str | None, cfg_hash: str, now: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO knowledge_bases(id,name,description,config_hash,created_at,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                description=excluded.description, config_hash=excluded.config_hash, updated_at=excluded.updated_at""",
                (kb_id, name, description, cfg_hash, now, now),
            )
            self.conn.commit()

    def upsert_source(self, source_id: str, kb_id: str, name: str, source_type: str, path: str, enabled: bool, config: dict[str, Any], status: str = "registered") -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO sources(id,knowledge_base_id,name,type,path,enabled,config_json,last_status)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                type=excluded.type,path=excluded.path,enabled=excluded.enabled,config_json=excluded.config_json,last_status=excluded.last_status""",
                (source_id, kb_id, name, source_type, path, int(enabled), json.dumps(config, default=str), status),
            )
            self.conn.commit()

    def replace_artifact_chunks(self, artifact: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO artifacts(id,source_id,type,path,relative_path,mime_type,size_bytes,sha256,mtime,git_branch,git_commit,last_indexed_at,status)
                    VALUES(:id,:source_id,:type,:path,:relative_path,:mime_type,:size_bytes,:sha256,:mtime,:git_branch,:git_commit,:last_indexed_at,:status)
                    ON CONFLICT(id) DO UPDATE SET type=excluded.type,path=excluded.path,relative_path=excluded.relative_path,
                    mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,sha256=excluded.sha256,mtime=excluded.mtime,
                    git_branch=excluded.git_branch,git_commit=excluded.git_commit,last_indexed_at=excluded.last_indexed_at,status=excluded.status""",
                    artifact,
                )
                self.conn.execute("DELETE FROM chunks WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute("DELETE FROM chunks_fts WHERE artifact_id=?", (artifact["id"],))
                for chunk in chunks:
                    self.conn.execute(
                        """INSERT INTO chunks(id,artifact_id,source_id,chunk_index,heading_path,start_line,end_line,text,text_hash,summary,token_estimate)
                        VALUES(:id,:artifact_id,:source_id,:chunk_index,:heading_path,:start_line,:end_line,:text,:text_hash,:summary,:token_estimate)""",
                        chunk,
                    )
                    self.conn.execute(
                        "INSERT INTO chunks_fts(chunk_id,source_id,artifact_id,title,path,heading_path,text) VALUES(?,?,?,?,?,?,?)",
                        (chunk["id"], chunk["source_id"], chunk["artifact_id"], artifact["relative_path"] or artifact["path"], artifact["relative_path"] or artifact["path"], chunk.get("heading_path") or "", chunk["text"]),
                    )

    def mark_source_indexed(self, source_id: str, now: str, status: str = "healthy") -> None:
        with self._lock:
            self.conn.execute("UPDATE sources SET last_indexed_at=?, last_status=? WHERE id=?", (now, status, source_id))
            self.conn.commit()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
