from __future__ import annotations

from syncsage.persistence.state_store import StateStore


class SearchStore:
    def __init__(self, state: StateStore):
        self.state = state

    def search(self, query: str, source_name: str | None = None, max_results: int = 10) -> list[dict]:
        params: list[object] = [query]
        where = "chunks_fts MATCH ?"
        if source_name:
            where += " AND source_id = ?"
            params.append(source_name)
        params.append(max_results)
        sql = f"""
        SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id, chunks_fts.path,
               chunks_fts.heading_path, chunks.text, chunks.start_line, chunks.end_line,
               artifacts.path AS absolute_path, artifacts.relative_path, bm25(chunks_fts) AS rank_score
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
                """SELECT chunks.id AS chunk_id, chunks.source_id, chunks.artifact_id, artifacts.relative_path AS path,
                chunks.heading_path, chunks.text, chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                artifacts.relative_path, 0.0 AS rank_score FROM chunks JOIN artifacts ON artifacts.id=chunks.artifact_id
                WHERE chunks.text LIKE ? OR artifacts.relative_path LIKE ? LIMIT ?""",
                (f"%{query}%", f"%{query}%", max_results),
            )
        results = []
        for i, row in enumerate(rows, start=1):
            results.append(
                {
                    "rank": i,
                    "node_id": row["artifact_id"],
                    "chunk_id": row["chunk_id"],
                    "type": "chunk",
                    "title": row["relative_path"],
                    "path": row["absolute_path"],
                    "relative_path": row["relative_path"],
                    "score": float(1.0 / (1.0 + abs(row["rank_score"] or 0.0))),
                    "reason": "SQLite FTS/path match",
                    "summary": (row["text"] or "")[:240],
                    "chunks": [
                        {
                            "chunk_id": row["chunk_id"],
                            "start_line": row["start_line"],
                            "end_line": row["end_line"],
                            "text_preview": (row["text"] or "")[:500],
                        }
                    ],
                    "provenance": {"source_id": row["source_id"], "path": row["absolute_path"], "relative_path": row["relative_path"]},
                }
            )
        return results
