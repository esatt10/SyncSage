from __future__ import annotations

from syncsage.persistence.state_store import StateStore


class KnowledgeBaseRegistry:
    def __init__(self, state: StateStore):
        self.state = state

    def list(self) -> list[dict]:
        rows = self.state.rows(
            """SELECT kb.id, kb.name, kb.description, kb.updated_at,
            COUNT(s.id) AS source_count,
            COALESCE(MAX(s.last_status), 'registered') AS status,
            MAX(s.last_indexed_at) AS last_indexed_at
            FROM knowledge_bases kb LEFT JOIN sources s ON s.knowledge_base_id=kb.id
            GROUP BY kb.id ORDER BY kb.name"""
        )
        return [dict(row) for row in rows]
