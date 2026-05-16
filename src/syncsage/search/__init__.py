from __future__ import annotations

from syncsage.config.schema import SyncSageConfig
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.search.hybrid import HybridSearch
from syncsage.search.sqlite_store import SearchStore


class SearchEngine(HybridSearch):
    def __init__(self, config: SyncSageConfig):
        paths = StatePaths.from_config(config)
        state = StateStore(paths.sqlite)
        state.migrate()
        super().__init__(SearchStore(state))
        self.config = config

    def search(self, query: str) -> dict:
        return self.search_context(self.config.knowledge_base_id, query)
