"""A searchable index of graph nodes, in SQLite.

Graph-mode search used to score every node in memory. That is O(nodes) per
query and it does not care how selective the query is: on a 500k-node graph a
single search cost 2.5s idle and over 10s while indexing ran, and the agent
loop issues several per question. Chunk text has never had this problem —
it lives in an FTS5 index — so graph nodes get the same treatment.

The index is a **cache derived from the graph**, never a second source of
truth. It can be deleted at any time and rebuilt from the in-memory graph, and
graph search falls back to the in-memory scan whenever it is empty or
unavailable. That keeps the graph the only thing that has to be correct.

One semantic change, deliberate: FTS matches tokens and prefixes where the old
scan matched arbitrary substrings, so a query for ``exec`` finds
``executor`` (token prefix) but no longer finds ``myexecutor`` (infix). This
is the same matching the text search over chunks has always used, and the
candidates it returns are still ranked by the original scorer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts USING fts5(
  node_id UNINDEXED,
  source_id UNINDEXED,
  body,
  tokenize='unicode61'
);
"""

#: Postgres has no FTS5. Same table name and columns so every write below is
#: unchanged, plus a generated search vector and its GIN index. ``simple``
#: (not ``english``) matches FTS5's unicode61 tokenizer: no stemming, no
#: stopword removal — stemming here would silently change which graph nodes a
#: query reaches.
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes_fts (
  node_id TEXT PRIMARY KEY,
  source_id TEXT,
  body TEXT,
  search_vector tsvector GENERATED ALWAYS AS (
    to_tsvector('simple', coalesce(body, ''))
  ) STORED
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_fts_vector
  ON graph_nodes_fts USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_fts_source ON graph_nodes_fts(source_id);
"""

#: FTS5 treats these as syntax; a query built from user text must not.
_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)


class NodeIndex:
    """Full-text index over graph node text, backed by the state database."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self._ready = False
        dialect = getattr(state, "dialect", None)
        self._postgres = bool(dialect is not None and dialect.is_postgres)
        # Sticky "there is something in here". A query only needs to know
        # empty-vs-not, and COUNT(*) over an FTS5 table is a full scan — on a
        # 577k-row index that was costing more than the search it guarded.
        # Only ever flips false→true, and writes set it directly.
        self._populated = False

    def ensure(self) -> bool:
        """Create the table if needed. False when FTS5 is unavailable."""

        if self._ready:
            return True
        try:
            self.state.conn.executescript(POSTGRES_SCHEMA if self._postgres else SCHEMA)
            self.state.conn.commit()
            self._ready = True
        except Exception as exc:  # pragma: no cover - SQLite without FTS5
            logger.warning("graph node index unavailable (%s); using the in-memory scan", exc)
            self._ready = False
        return self._ready

    def count(self) -> int:
        """Exact row count. Callers on the query path want :meth:`populated`."""

        if not self.ensure():
            return 0
        rows = self.state.rows("SELECT COUNT(*) AS n FROM graph_nodes_fts")
        total = int(rows[0]["n"]) if rows else 0
        self._populated = total > 0
        return total

    def populated(self) -> bool:
        """Is there anything to search? One row, not a count."""

        if self._populated:
            return True
        if not self.ensure():
            return False
        probe = "node_id" if self._postgres else "rowid"
        self._populated = bool(self.state.rows(f"SELECT {probe} FROM graph_nodes_fts LIMIT 1"))
        return self._populated

    def replace_all(self, nodes: Iterable[tuple[str, str, str]]) -> int:
        """Rebuild from scratch. ``nodes`` yields (node_id, source_id, body)."""

        if not self.ensure():
            return 0
        written = 0
        with self.state.conn:
            self.state.conn.execute("DELETE FROM graph_nodes_fts")
            for batch in _batched(nodes, 2000):
                self.state.conn.executemany(
                    "INSERT INTO graph_nodes_fts (node_id, source_id, body) VALUES (?,?,?)",
                    batch,
                )
                written += len(batch)
        self._populated = written > 0
        return written

    def apply(self, upserts: Iterable[tuple[str, str, str]], removals: Iterable[str]) -> int:
        """Incremental update: delete then re-insert the touched rows."""

        if not self.ensure():
            return 0
        upserts = list(upserts)
        touched = [node_id for node_id, _source, _body in upserts]
        touched.extend(removals)
        if not touched:
            return 0
        with self.state.conn:
            for batch in _batched(iter(touched), 500):
                placeholders = ",".join("?" for _ in batch)
                self.state.conn.execute(
                    f"DELETE FROM graph_nodes_fts WHERE node_id IN ({placeholders})",
                    tuple(batch),
                )
            for batch in _batched(iter(upserts), 2000):
                self.state.conn.executemany(
                    "INSERT INTO graph_nodes_fts (node_id, source_id, body) VALUES (?,?,?)",
                    batch,
                )
        if upserts:
            self._populated = True
        return len(upserts)

    def candidates(
        self,
        tokens: list[str],
        source_name: str | None = None,
        limit: int = 2000,
    ) -> list[str] | None:
        """Node ids whose text matches any token. None = index cannot answer.

        ``None`` (rather than an empty list) when the index is unavailable or
        empty, so the caller can fall back to scanning instead of concluding
        that nothing matched.
        """

        if not self.ensure():
            return None
        match = _match_expression(tokens)
        if not match:
            return None
        if not self.populated():
            return None
        if self._postgres:
            # tsquery, not MATCH. Prefix search is `word:*`, and the terms are
            # OR'd exactly as the FTS5 expression does — same recall, different
            # spelling. Building this from the already-sanitized tokens rather
            # than translating the FTS5 string keeps one source of truth for
            # what a term is.
            sql = (
                "SELECT node_id FROM graph_nodes_fts, to_tsquery('simple', ?) q "
                "WHERE search_vector @@ q"
            )
            params: list[Any] = [_tsquery_expression(tokens)]
        else:
            sql = "SELECT node_id FROM graph_nodes_fts WHERE graph_nodes_fts MATCH ?"
            params = [match]
        if source_name:
            sql += " AND source_id = ?"
            params.append(source_name)
        sql += " LIMIT ?"
        params.append(int(limit))
        try:
            found = [str(row["node_id"]) for row in self.state.rows(sql, tuple(params))]
        except Exception as exc:  # a malformed MATCH must not fail the search
            logger.debug("node index query failed (%s); falling back to scan", exc)
            return None
        if found:
            return found
        # No hits is ambiguous: either nothing matches, or the index was
        # emptied behind our back (a wipe, a fresh state dir). Distinguish the
        # two with one cheap row probe, so an emptied index degrades to
        # scanning instead of silently answering "nothing found".
        self._populated = False
        return found if self.populated() else None


def _match_expression(tokens: list[str]) -> str:
    """An OR of prefix terms, with FTS5 syntax characters stripped."""

    terms = []
    for token in tokens:
        cleaned = _UNSAFE.sub(" ", token).strip()
        for word in cleaned.split():
            if len(word) > 1:
                terms.append(f'"{word}"*')
    return " OR ".join(dict.fromkeys(terms))


def _batched(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _tsquery_expression(tokens: list[str]) -> str:
    """The Postgres twin of :func:`_match_expression`.

    Same tokens, same OR semantics, same prefix matching — spelled ``word:*``
    and joined with ``|`` instead of FTS5's ``"word"* OR``. Built from the
    tokens rather than by rewriting the FTS5 string so there is one definition
    of what counts as a term.
    """

    terms = []
    for token in tokens:
        cleaned = _UNSAFE.sub(" ", token).strip()
        for word in cleaned.split():
            if len(word) > 1:
                terms.append(f"{word}:*")
    return " | ".join(dict.fromkeys(terms))
