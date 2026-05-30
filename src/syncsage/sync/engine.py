from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from syncsage.config.schema import SourceConfig, SyncSageConfig
from syncsage.graph.builder import GraphBuilder
from syncsage.ingestion.pipeline import git_state, parse_connector_payload, utc_now
from syncsage.persistence.graph_store import GraphStore
from syncsage.persistence.manifest import ManifestStore
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.registry.source_registry import SourceRegistry
from syncsage.sync.connectors import ConnectorItem, connector_for_source
from syncsage.sync.locks import source_lock

SyncMode = Literal["incremental", "full", "validate_only", "repair"]
SYNC_MODES = {"incremental", "full", "validate_only", "repair"}


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    indexed_artifacts: int
    skipped_artifacts: int
    graph_nodes: int
    graph_edges: int
    status: str = "healthy"
    details: dict = field(default_factory=dict)


class SyncEngine:
    def __init__(
        self,
        config: SyncSageConfig,
        paths: StatePaths | None = None,
        state: StateStore | None = None,
    ):
        self.config = config
        self.paths = paths or StatePaths.from_config(config)
        self.paths.ensure()
        self.state = state or StateStore(self.paths.sqlite)
        self.state.migrate()
        SourceRegistry(self.config, self.state).initialize()
        self.manifests = ManifestStore(self.paths.manifests)
        self.graph_store = GraphStore(self.paths.graphs)
        self.graph_builder = GraphBuilder(config)
        existing = self.graph_store.load(config.knowledge_base_id)
        if len(existing):
            self.graph_builder.graph = existing

    def startup(self) -> list[SyncResult]:
        self.paths.ensure()
        self.state.migrate()
        SourceRegistry(self.config, self.state).initialize()
        results = []
        for source in self.config.sources:
            if source.enabled and source.sync.on_startup:
                results.append(self.sync_source(source.name, "incremental"))
        return results

    def sync_all(self, mode: SyncMode = "incremental") -> list[SyncResult]:
        return [
            self.sync_source(source.name, mode)
            for source in self.config.sources
            if source.enabled
        ]

    def sync_source(self, source_name: str, mode: SyncMode = "incremental") -> SyncResult:
        if mode not in SYNC_MODES:
            raise ValueError(f"Unsupported sync mode: {mode}")
        source = self._source(source_name)
        with source_lock(self.paths.locks, source.name):
            connector = connector_for_source(source, self.state)
            if mode == "validate_only":
                health = connector.validate()
                status = "validated" if health.ok else health.status
                self.state.mark_source_status(source.name, status)
                return SyncResult(
                    source.name,
                    0,
                    0,
                    self.graph_builder.graph.number_of_nodes(),
                    self.graph_builder.graph.number_of_edges(),
                    status,
                    {
                        "connector_type": connector.connector_type,
                        "item_count": health.item_count,
                        "checked_items": health.checked_items,
                        "errors": health.errors,
                        "checkpoint": health.checkpoint,
                    },
                )
            manifest = self.manifests.load(source.name)
            artifacts = manifest.setdefault("artifacts", {})
            items = connector.list_items()
            if mode == "full":
                self.state.delete_source_artifacts(source.name)
                self.graph_builder.remove_source_content(source.name)
                manifest = {"source_id": source.name, "artifacts": {}}
                artifacts = manifest["artifacts"]
            indexed = 0
            skipped = 0
            git_metadata = (
                git_state(source.path)
                if source.type.value == "repository"
                else None
            )
            for item in items:
                previous = artifacts.get(item.relative_path)
                if self._can_skip_before_read(mode, source.name, previous, item):
                    skipped += 1
                    continue
                payload = connector.read_item(item)
                parsed = parse_connector_payload(source, item, payload, git_metadata)
                if parsed is None:
                    continue
                if (
                    mode == "incremental"
                    and previous
                    and previous.get("sha256") == parsed.sha256
                    and self._graph_has_indexed_artifact(source.name, previous)
                ):
                    skipped += 1
                    continue
                if mode == "repair" and not self._needs_repair(
                    source.name,
                    parsed.id,
                    parsed.sha256,
                    previous,
                    len(parsed.chunks),
                ):
                    skipped += 1
                    continue
                now = utc_now()
                artifact_row = {
                    "id": parsed.id,
                    "source_id": parsed.source_id,
                    "type": parsed.type,
                    "path": str(parsed.path),
                    "relative_path": parsed.relative_path,
                    "mime_type": parsed.mime_type,
                    "size_bytes": parsed.size_bytes,
                    "sha256": parsed.sha256,
                    "mtime": parsed.mtime,
                    "git_branch": parsed.git_branch,
                    "git_commit": parsed.git_commit,
                    "last_indexed_at": now,
                    "status": "indexed",
                }
                chunk_rows = [
                    {
                        "id": (
                            f"chunk:{source.name}:{parsed.relative_path}:"
                            f"sha256={chunk.text_hash}:chunk={chunk.index:04d}"
                        ),
                        "artifact_id": parsed.id,
                        "source_id": source.name,
                        "chunk_index": chunk.index,
                        "heading_path": chunk.heading_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                        "summary": chunk.text[:240],
                        "token_estimate": chunk.token_estimate,
                    }
                    for chunk in parsed.chunks
                ]
                self.state.replace_artifact_chunks(artifact_row, chunk_rows)
                enrichment = self.graph_builder.add_artifact(source, parsed)
                self.state.replace_artifact_enrichment(
                    parsed.id,
                    parsed.source_id,
                    enrichment.terms,
                    enrichment.symbols,
                )
                artifacts[parsed.relative_path] = {
                    "sha256": parsed.sha256,
                    "artifact_id": parsed.id,
                    "last_indexed_at": now,
                    "git_branch": parsed.git_branch,
                    "git_commit": parsed.git_commit,
                }
                indexed += 1
            self.graph_builder.add_similarity_edges(source.name)
            manifest["last_indexed_at"] = utc_now()
            manifest["connector"] = {"type": connector.connector_type}
            self.manifests.save(source.name, manifest)
            self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
            now = utc_now()
            cursor, high_watermark = connector.checkpoint_from_items(items)
            high_watermark.update({"indexed_artifacts": indexed, "skipped_artifacts": skipped})
            connector.set_checkpoint(cursor, high_watermark, "healthy")
            self.state.mark_source_indexed(source.name, now)
            return SyncResult(
                source.name,
                indexed,
                skipped,
                self.graph_builder.graph.number_of_nodes(),
                self.graph_builder.graph.number_of_edges(),
                "healthy",
                {
                    "connector_type": connector.connector_type,
                    "checkpoint": self.state.get_source_checkpoint(source.name),
                },
            )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "node_count": self.graph_builder.graph.number_of_nodes(),
            "edge_count": self.graph_builder.graph.number_of_edges(),
            "artifact_count": int(self.state.rows("SELECT COUNT(*) AS c FROM artifacts")[0]["c"]),
            "chunk_count": int(self.state.rows("SELECT COUNT(*) AS c FROM chunks")[0]["c"]),
        }

    def search_context(self, query: str, max_results: int = 10) -> dict:
        from syncsage.search.hybrid import HybridSearch
        from syncsage.search.sqlite_store import SearchStore

        return HybridSearch(SearchStore(self.state)).search_context(
            self.config.knowledge_base_id,
            query,
            max_results=max_results,
        )

    def export_obsidian_notes(
        self,
        vault_path=None,
        preview: bool = False,
        template_profile: str | None = None,
    ) -> dict:
        from syncsage.obsidian.exporter import ObsidianExporter

        if vault_path is not None:
            self.config.syncsage.vault_path = vault_path
        return ObsidianExporter(self.config, self.state).export(
            preview=preview,
            template_profile=template_profile,
        )

    def _source(self, name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == name:
                return source
        raise KeyError(f"Unknown source: {name}")

    def _can_skip_before_read(
        self,
        mode: SyncMode,
        source_name: str,
        previous: dict | None,
        item: ConnectorItem,
    ) -> bool:
        if (
            mode != "incremental"
            or previous is None
            or item.sha256 is None
            or previous.get("sha256") != item.sha256
        ):
            return False
        return self._graph_has_indexed_artifact(source_name, previous)

    def _graph_has_indexed_artifact(self, source_name: str, previous: dict) -> bool:
        artifact_id = previous.get("artifact_id")
        if not artifact_id:
            return False
        graph = self.graph_builder.graph
        source_node = f"source:{self.config.knowledge_base_id}:{source_name}"
        if not graph.has_node(source_node) or not graph.has_node(artifact_id):
            return False
        edge_map = graph.get_edge_data(source_node, artifact_id, default={})
        return any(data.get("type") == "indexes" for data in edge_map.values())

    def _needs_repair(
        self,
        source_id: str,
        artifact_id: str,
        sha256: str,
        previous: dict | None,
        expected_chunks: int,
    ) -> bool:
        if not previous or previous.get("sha256") != sha256:
            return True
        state = self.state.artifact_state(source_id, artifact_id)
        if state is None:
            return True
        return (
            state.get("sha256") != sha256
            or state.get("status") != "indexed"
            or int(state.get("chunk_count") or 0) < expected_chunks
        )
