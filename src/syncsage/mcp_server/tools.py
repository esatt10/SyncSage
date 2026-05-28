from __future__ import annotations

from syncsage.config.schema import SourceConfig, SourceType, SyncSageConfig
from syncsage.obsidian.exporter import ObsidianExporter
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.registry.knowledge_base_registry import KnowledgeBaseRegistry
from syncsage.registry.source_registry import SourceRegistry
from syncsage.search.hybrid import HybridSearch
from syncsage.search.sqlite_store import SearchStore
from syncsage.security.path_policy import resolve_under
from syncsage.sync.engine import SyncEngine


class SyncSageTools:
    def __init__(self, config: SyncSageConfig):
        self.config = config
        self.paths = StatePaths.from_config(config)
        self.paths.ensure()
        self.state = StateStore(self.paths.sqlite)
        self.state.migrate()
        self.engine = SyncEngine(config, self.paths, self.state)
        self.searcher = HybridSearch(SearchStore(self.state))

    def list_knowledge_bases(self) -> dict:
        return {"knowledge_bases": KnowledgeBaseRegistry(self.state).list()}

    def register_source(
        self,
        knowledge_base: str,
        name: str,
        source_type: str,
        path: str,
        description: str | None = None,
        enabled: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        source = SourceConfig(
            name=name,
            type=SourceType(source_type),
            path=resolve_under(
                path,
                [self.config.syncsage.workspace_root, self.config.syncsage.vault_path],
            ),
            description=description,
            enabled=enabled,
        )
        if include is not None:
            source.include = include
        if exclude is not None:
            source.exclude = exclude
        SourceRegistry(self.config, self.state).register_source(source)
        self.config.sources = [
            existing for existing in self.config.sources if existing.name != source.name
        ]
        self.config.sources.append(source)
        return {
            "status": "registered",
            "knowledge_base": self.config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "provenance": {
                "registered_by": "mcp",
                "persistence": "runtime_state",
                "config_update_required": True,
            },
        }

    def sync_source(self, knowledge_base: str, source_name: str, mode: str = "incremental") -> dict:
        self._require_knowledge_base(knowledge_base)
        return self.engine.sync_source(source_name, mode).__dict__  # type: ignore[arg-type]

    def sync_all(self, knowledge_base: str, mode: str = "incremental") -> dict:
        self._require_knowledge_base(knowledge_base)
        return {"results": [r.__dict__ for r in self.engine.sync_all(mode)]}  # type: ignore[arg-type]

    def search_context(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        include_chunks: bool = True,
        include_graph_neighbors: bool = True,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return self.searcher.search_context(
            knowledge_base or self.config.knowledge_base_id,
            query,
            mode,
            max_results,
        )

    def get_relevant_files(
        self,
        knowledge_base: str,
        task: str,
        source_name: str | None = None,
        max_files: int = 8,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        payload = self.searcher.search_context(
            knowledge_base or self.config.knowledge_base_id,
            task,
            "hybrid",
            max_files,
            source_name,
        )
        return {"files": payload["results"]}

    def get_graph_neighbors(
        self,
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        graph = self.engine.graph_builder.graph
        if node_id not in graph:
            return {"node_id": node_id, "neighbors": []}
        neighbors = []
        for target in graph.neighbors(node_id):
            edge_data = graph.get_edge_data(node_id, target, default={})
            types = [data.get("type") for data in edge_data.values()]
            if edge_types and not set(types).intersection(edge_types):
                continue
            neighbors.append(
                {"node_id": target, "edge_types": types, "node": dict(graph.nodes[target])}
            )
        return {"node_id": node_id, "depth": depth, "neighbors": neighbors}

    def get_file_summary(
        self,
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        rows = self.state.rows(
            """SELECT artifacts.*, GROUP_CONCAT(chunks.summary, '\n') AS summary FROM artifacts
        LEFT JOIN chunks ON chunks.artifact_id=artifacts.id
        WHERE artifacts.relative_path=? AND (? IS NULL OR artifacts.source_id=?)
        GROUP BY artifacts.id LIMIT 1""",
            (path, source_name, source_name),
        )
        return dict(rows[0]) if rows else {"path": path, "summary": None}

    def get_repo_map(self, knowledge_base: str, source_name: str, depth: int = 3) -> dict:
        self._require_knowledge_base(knowledge_base)
        rows = self.state.rows(
            """SELECT relative_path,type,size_bytes
            FROM artifacts WHERE source_id=? ORDER BY relative_path""",
            (source_name,),
        )
        return {"source_name": source_name, "files": [dict(row) for row in rows]}

    def explain_node(self, knowledge_base: str, node_id: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        graph = self.engine.graph_builder.graph
        if node_id not in graph:
            return {"node_id": node_id, "explanation": "Node is not present in the current graph."}
        node = dict(graph.nodes[node_id])
        return {
            "node_id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "explanation": f"{node.get('label')} is a {node.get('type')} node indexed by SyncSage.",
            "provenance": node.get("provenance"),
        }

    def export_obsidian_notes(
        self,
        knowledge_base: str,
        source_name: str | None = None,
        scope: str = "knowledge_base",
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return ObsidianExporter(self.config, self.state).export(source_name)

    def get_sync_status(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "sources": SourceRegistry(self.config, self.state).list_sources(),
            "checkpoints": self.state.list_source_checkpoints(),
        }

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:
        if knowledge_base and knowledge_base != self.config.knowledge_base_id:
            raise ValueError(f"Unknown knowledge base: {knowledge_base}")
