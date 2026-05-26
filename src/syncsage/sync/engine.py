from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from syncsage.config.schema import SourceConfig, SyncSageConfig
from syncsage.graph.builder import GraphBuilder
from syncsage.ingestion.pipeline import discover_files, git_state, parse_file, utc_now
from syncsage.persistence.graph_store import GraphStore
from syncsage.persistence.manifest import ManifestStore
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.registry.source_registry import SourceRegistry
from syncsage.sync.locks import source_lock

SyncMode = Literal["incremental", "full", "validate_only", "repair"]


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    indexed_artifacts: int
    skipped_artifacts: int
    graph_nodes: int
    graph_edges: int
    status: str = "healthy"


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
        source = self._source(source_name)
        with source_lock(self.paths.locks, source.name):
            manifest = self.manifests.load(source.name)
            artifacts = manifest.setdefault("artifacts", {})
            indexed = 0
            skipped = 0
            git_metadata = (
                git_state(source.path)
                if source.type.value == "repository"
                else None
            )
            for path in discover_files(source):
                parsed = parse_file(source, path, git_metadata)
                if parsed is None:
                    continue
                previous = artifacts.get(parsed.relative_path)
                if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
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
                self.graph_builder.add_artifact(source, parsed)
                artifacts[parsed.relative_path] = {
                    "sha256": parsed.sha256,
                    "artifact_id": parsed.id,
                    "last_indexed_at": now,
                    "git_branch": parsed.git_branch,
                    "git_commit": parsed.git_commit,
                }
                indexed += 1
            manifest["last_indexed_at"] = utc_now()
            self.manifests.save(source.name, manifest)
            self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
            self.state.mark_source_indexed(source.name, utc_now())
            return SyncResult(
                source.name,
                indexed,
                skipped,
                self.graph_builder.graph.number_of_nodes(),
                self.graph_builder.graph.number_of_edges(),
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

    def export_obsidian_notes(self, vault_path=None) -> dict:
        from syncsage.obsidian.exporter import ObsidianExporter

        if vault_path is not None:
            self.config.syncsage.vault_path = vault_path
        return ObsidianExporter(self.config, self.state).export()

    def _source(self, name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == name:
                return source
        raise KeyError(f"Unknown source: {name}")
