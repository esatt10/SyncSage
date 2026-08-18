from __future__ import annotations

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.search.vector_store import vector_searcher_from_config


class SearchEngine(HybridSearch):
    def __init__(self, config: PheasantConfig):
        paths = StatePaths.from_config(config)
        state = StateStore.from_config(config, paths.sqlite)
        state.migrate()
        super().__init__(SearchStore(state), vector=vector_searcher_from_config(config, state))
        self.config = config

    def search(self, query: str) -> dict:
        return self.search_context(self.config.knowledge_base_id, query)
