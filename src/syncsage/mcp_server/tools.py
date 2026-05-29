from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path

import yaml

from syncsage.config.schema import SourceConfig, SourceType, SyncSageConfig
from syncsage.ingestion.pipeline import utc_now
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
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        if self.config.security.allow_user_selected_source_paths:
            resolved_path = Path(path).expanduser().resolve()
            if not resolved_path.exists():
                raise ValueError(f"Path does not exist: {resolved_path}")
        else:
            resolved_path = resolve_under(
                path,
                [
                    self.config.syncsage.workspace_root,
                    self.config.syncsage.vault_path,
                    self.config.syncsage.exports_path,
                    *self.config.security.allow_workspace_roots,
                ],
            )
        source = SourceConfig(
            name=name,
            type=SourceType(source_type),
            path=resolved_path,
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
        created_at = utc_now()
        self._audit(
            source.name,
            "register_source",
            actor,
            transport,
            client_id,
            created_at,
            {"source": source.model_dump(mode="json")},
        )
        return {
            "status": "registered",
            "knowledge_base": self.config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "provenance": {
                "registered_by": actor,
                "registered_at": created_at,
                "transport": transport,
                "client_id": client_id,
                "persistence": "runtime_state",
                "config_update_required": True,
            },
        }

    def list_sources(
        self,
        knowledge_base: str,
        enabled: bool | None = None,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "sources": SourceRegistry(self.config, self.state).list_sources(
                enabled=enabled,
                status=status,
                source_type=source_type,
                limit=limit,
                offset=offset,
            ),
            "pagination": {"limit": limit, "offset": offset},
        }

    def disable_source(
        self,
        knowledge_base: str,
        source_name: str,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        self.state.set_source_enabled(source_name, False, "disabled")
        for source in self.config.sources:
            if source.name == source_name:
                source.enabled = False
        self._audit(source_name, "disable_source", actor, transport, client_id, utc_now())
        return {"status": "disabled", "source_name": source_name}

    def remove_source(
        self,
        knowledge_base: str,
        source_name: str,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        self.engine.graph_builder.remove_source_content(source_name)
        self.engine.graph_store.save(self.config.knowledge_base_id, self.engine.graph_builder.graph)
        self.engine.manifests.delete(source_name)
        self.state.delete_source(source_name)
        self.config.sources = [
            source for source in self.config.sources if source.name != source_name
        ]
        self._audit(source_name, "remove_source", actor, transport, client_id, utc_now())
        return {"status": "removed", "source_name": source_name}

    def promote_runtime_source_to_config(
        self,
        knowledge_base: str,
        source_name: str,
        config_path: str | None = None,
        write: bool = False,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        source = self._source_config(source_name)
        source_payload = source.model_dump(mode="json")
        yaml_patch = yaml.safe_dump({"sources": [source_payload]}, sort_keys=False)
        wrote = False
        if write:
            if not config_path:
                raise ValueError("config_path is required when write=True")
            path = Path(config_path)
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            data = data or {}
            existing = [
                item
                for item in data.get("sources", []) or []
                if item.get("name") != source.name
            ]
            data["sources"] = [*existing, source_payload]
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            wrote = True
        self._audit(
            source_name,
            "promote_runtime_source_to_config",
            actor,
            transport,
            client_id,
            utc_now(),
            {"write": wrote, "config_path": config_path},
        )
        return {
            "status": "promoted" if wrote else "patch_generated",
            "source_name": source_name,
            "yaml_patch": yaml_patch,
            "wrote_config": wrote,
            "config_path": config_path,
        }

    def sync_source(self, knowledge_base: str, source_name: str, mode: str = "incremental") -> dict:
        self._require_knowledge_base(knowledge_base)
        result = self.engine.sync_source(source_name, mode).__dict__  # type: ignore[arg-type]
        self._audit(source_name, "sync_source", "mcp", "mcp", None, utc_now(), result)
        return result

    def sync_all(self, knowledge_base: str, mode: str = "incremental") -> dict:
        self._require_knowledge_base(knowledge_base)
        results = [r.__dict__ for r in self.engine.sync_all(mode)]  # type: ignore[arg-type]
        for result in results:
            self._audit(result["source_id"], "sync_source", "mcp", "mcp", None, utc_now(), result)
        return {"results": results}

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
        max_depth = max(1, min(int(depth or 1), 10))
        allowed = set(edge_types or [])
        queue = deque([(node_id, 0, [node_id])])
        visited = {node_id}
        neighbors = []
        while queue:
            current, current_depth, path = queue.popleft()
            if current_depth >= max_depth:
                continue
            for _source, target, edge_map in graph.out_edges(current):
                matching_edges = [
                    data
                    for data in edge_map.values()
                    if not allowed or data.get("type") in allowed
                ]
                if not matching_edges:
                    continue
                next_depth = current_depth + 1
                edge_type_values = sorted(
                    {data.get("type") for data in matching_edges if data.get("type")}
                )
                neighbors.append(
                    {
                        "node_id": target,
                        "depth": next_depth,
                        "edge_types": edge_type_values,
                        "path": [*path, target],
                        "node": dict(graph.nodes[target]),
                    }
                )
                if target not in visited:
                    visited.add(target)
                    queue.append((target, next_depth, [*path, target]))
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
        preview: bool = False,
        template_profile: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return ObsidianExporter(self.config, self.state).export(
            source_name,
            preview=preview,
            template_profile=template_profile,
        )

    def get_sync_status(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "sources": SourceRegistry(self.config, self.state).list_sources(),
            "checkpoints": self.state.list_source_checkpoints(),
        }

    def get_source_manifest(self, knowledge_base: str, source_name: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        return self.engine.manifests.load(source_name)

    def get_sync_history(
        self,
        knowledge_base: str,
        source_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "events": self.state.list_source_audit_events(source_name, limit, offset),
            "pagination": {"limit": limit, "offset": offset},
        }

    def get_graph_slice(
        self,
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        edge_types: list[str] | None = None,
        limit: int = 100,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        traversal = self.get_graph_neighbors(knowledge_base, node_id, depth, edge_types)
        graph = self.engine.graph_builder.graph
        node_ids = [node_id] + [item["node_id"] for item in traversal["neighbors"][:limit]]
        node_set = set(node_ids)
        links = []
        for source in node_set:
            for _src, target, edge_map in graph.out_edges(source):
                if target not in node_set:
                    continue
                for key, data in edge_map.items():
                    if edge_types and data.get("type") not in set(edge_types):
                        continue
                    links.append({"source": source, "target": target, "key": key, **data})
        return {
            "node_id": node_id,
            "depth": depth,
            "nodes": [dict(graph.nodes[item]) for item in node_ids if item in graph],
            "links": links,
        }

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:
        if knowledge_base and knowledge_base != self.config.knowledge_base_id:
            raise ValueError(f"Unknown knowledge base: {knowledge_base}")

    def _require_source(self, source_name: str) -> None:
        if not self.state.get_source(source_name):
            raise KeyError(f"Unknown source: {source_name}")

    def _source_config(self, source_name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == source_name:
                return source
        row = self.state.get_source(source_name)
        if not row:
            raise KeyError(f"Unknown source: {source_name}")
        payload = json.loads(row["config_json"])
        return SyncSageConfig.model_validate({"sources": [payload]}).sources[0]

    def _audit(
        self,
        source_id: str | None,
        action: str,
        actor: str | None,
        transport: str | None,
        client_id: str | None,
        created_at: str,
        details: dict | None = None,
    ) -> None:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "source_id": source_id,
                    "action": action,
                    "created_at": created_at,
                    "details": details or {},
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        ordinal = len(self.state.list_source_audit_events(source_id, limit=10000))
        self.state.append_source_audit_event(
            f"audit:{digest}:{ordinal}",
            source_id,
            action,
            actor,
            transport,
            client_id,
            created_at,
            details,
        )
