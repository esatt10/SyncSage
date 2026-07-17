from __future__ import annotations

import re

from syncsage.persistence.state_store import StateStore


class SearchStore:
    def __init__(self, state: StateStore):
        self.state = state

    def search(
        self,
        query: str,
        source_name: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        # Natural-language queries ("where does the X service run") must not
        # fall to FTS5's implicit-AND — a single unmatched stopword would
        # zero out the whole query (found by the Step-33.4 memory benchmark).
        # OR the sanitized tokens instead and let BM25 rank by token rarity;
        # quoting each token also neutralizes FTS5 query-syntax characters.
        tokens = _query_tokens(query)
        match_expr = " OR ".join(f'"{token}"' for token in tokens) if tokens else query
        params: list[object] = [match_expr]
        where = "chunks_fts MATCH ?"
        if source_name:
            where += " AND source_id = ?"
            params.append(source_name)
        params.append(max_results)
        sql = f"""
        SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
               chunks_fts.path, chunks_fts.heading_path, chunks.text,
               chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
               artifacts.relative_path, bm25(chunks_fts) AS rank_score
        FROM chunks_fts
        JOIN chunks ON chunks.id = chunks_fts.chunk_id
        JOIN artifacts ON artifacts.id = chunks_fts.artifact_id
        WHERE {where}
        ORDER BY rank_score LIMIT ?
        """
        try:
            rows = self.state.rows(sql, tuple(params))
        except Exception:
            rows = self.state.rows(
                """SELECT chunks.id AS chunk_id, chunks.source_id, chunks.artifact_id,
                artifacts.relative_path AS path, chunks.heading_path, chunks.text,
                chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                artifacts.relative_path, 0.0 AS rank_score
                FROM chunks JOIN artifacts ON artifacts.id=chunks.artifact_id
                WHERE chunks.text LIKE ? OR artifacts.relative_path LIKE ? LIMIT ?""",
                (f"%{query}%", f"%{query}%", max_results),
            )
        results = []
        seen_artifacts: set[str] = set()
        for i, row in enumerate(rows, start=1):
            seen_artifacts.add(row["artifact_id"])
            # FTS5 bm25() is a cost: more negative = better. Map it to a
            # monotone [0, 1) relevance so downstream merges (hybrid mode)
            # keep FTS's own ordering — the old 1/(1+|bm25|) *inverted* it
            # (found by the Step-33.4 memory benchmark). The LIKE-fallback
            # rows carry rank_score 0.0 and keep their historical 1.0.
            raw_rank = float(row["rank_score"] or 0.0)
            score = (-raw_rank / (1.0 - raw_rank)) if raw_rank < 0 else 1.0
            results.append(_row_result(row, i, score, "SQLite FTS/path match"))
        remaining = max(0, max_results - len(results))
        if remaining:
            for row in self._term_augmented_rows(query, source_name, remaining, seen_artifacts):
                seen_artifacts.add(row["artifact_id"])
                results.append(
                    _row_result(
                        row,
                        len(results) + 1,
                        float(row["term_score"] or 0.35),
                        "Graph term expansion",
                    )
                )
        return results

    def _term_augmented_rows(
        self,
        query: str,
        source_name: str | None,
        max_results: int,
        seen_artifacts: set[str],
    ) -> list:
        tokens = _query_tokens(query)
        if not tokens:
            return []
        term_clauses = " OR ".join("artifact_terms.normalized_term LIKE ?" for _ in tokens)
        params: list[object] = [f"%{token}%" for token in tokens]
        where = f"({term_clauses})"
        if source_name:
            where += " AND artifacts.source_id = ?"
            params.append(source_name)
        if seen_artifacts:
            placeholders = ",".join("?" for _ in seen_artifacts)
            where += f" AND artifacts.id NOT IN ({placeholders})"
            params.extend(sorted(seen_artifacts))
        params.append(max_results)
        return self.state.rows(
            f"""SELECT
                    chunks.id AS chunk_id,
                    artifacts.source_id,
                    artifacts.id AS artifact_id,
                    artifacts.relative_path AS path,
                    chunks.heading_path,
                    chunks.text,
                    chunks.start_line,
                    chunks.end_line,
                    artifacts.path AS absolute_path,
                    artifacts.relative_path,
                    MAX(artifact_terms.weight) AS term_score,
                    GROUP_CONCAT(DISTINCT artifact_terms.term) AS matched_terms
                FROM artifact_terms
                JOIN artifacts ON artifacts.id = artifact_terms.artifact_id
                JOIN chunks ON chunks.artifact_id = artifacts.id
                WHERE {where}
                  AND chunks.chunk_index = (
                    SELECT MIN(c2.chunk_index)
                    FROM chunks AS c2
                    WHERE c2.artifact_id = artifacts.id
                  )
                GROUP BY artifacts.id
                ORDER BY term_score DESC, artifacts.relative_path
                LIMIT ?""",
            tuple(params),
        )


def _row_result(row, rank: int, score: float, reason: str) -> dict:
    return {
        "rank": rank,
        "node_id": row["artifact_id"],
        "chunk_id": row["chunk_id"],
        "type": "chunk",
        "title": row["relative_path"],
        "path": row["absolute_path"],
        "relative_path": row["relative_path"],
        "score": score,
        "reason": reason,
        "summary": (row["text"] or "")[:240],
        "chunks": [
            {
                "chunk_id": row["chunk_id"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "text_preview": (row["text"] or "")[:500],
            }
        ],
        "provenance": {
            "source_id": row["source_id"],
            "path": row["absolute_path"],
            "relative_path": row["relative_path"],
        },
    }


def _query_tokens(query: str) -> list[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", query):
        tokens.add(raw.lower())
        for part in re.split(r"[_\W]+", raw):
            if len(part) > 1:
                tokens.add(part.lower())
        camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
        for part in camel.split():
            if len(part) > 1:
                tokens.add(part.lower())
    return sorted(tokens)
