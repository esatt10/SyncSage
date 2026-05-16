from __future__ import annotations

from syncsage.search.sqlite_store import SearchStore


class HybridSearch:
    def __init__(self, store: SearchStore):
        self.store = store

    def search_context(self, knowledge_base: str, query: str, mode: str = "hybrid", max_results: int = 10, source_name: str | None = None) -> dict:
        return {
            "query": query,
            "knowledge_base": knowledge_base,
            "mode": mode,
            "results": self.store.search(query, source_name=source_name, max_results=max_results),
        }
