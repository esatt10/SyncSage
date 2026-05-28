from __future__ import annotations

from syncsage.graph.simple import SimpleMultiDiGraph

from syncsage.config.schema import SyncSageConfig, SourceConfig
from syncsage.ingestion.pipeline import ParsedArtifact, utc_now


class GraphBuilder:
    def __init__(self, config: SyncSageConfig):
        self.config = config
        self.graph = SimpleMultiDiGraph()
        self.kb_id = config.knowledge_base_id
        self.upsert_node(self.kb_id, "knowledge_base", self.kb_id, {})

    def upsert_node(self, node_id: str, node_type: str, label: str, attrs: dict) -> None:
        now = utc_now()
        existing_created = self.graph.nodes[node_id].get("created_at") if self.graph.has_node(node_id) else now
        self.graph.add_node(node_id, id=node_id, type=node_type, label=label, created_at=existing_created, updated_at=now, knowledge_base_id=self.kb_id, **attrs)

    def upsert_edge(self, source: str, target: str, edge_type: str, attrs: dict | None = None) -> None:
        attrs = attrs or {}
        for _key, data in self.graph.get_edge_data(source, target, default={}).items():
            if data.get("type") == edge_type:
                data.update(attrs)
                data["updated_at"] = utc_now()
                return
        self.graph.add_edge(source, target, type=edge_type, created_at=utc_now(), confidence=attrs.pop("confidence", 1.0), **attrs)

    def add_source(self, source: SourceConfig) -> str:
        node_id = f"source:{self.kb_id}:{source.name}"
        self.upsert_node(node_id, "source", source.name, {"source_id": source.name, "source_type": source.type.value, "path": str(source.path)})
        self.upsert_edge(self.kb_id, node_id, "contains", {"source_id": source.name})
        return node_id

    def add_artifact(self, source: SourceConfig, artifact: ParsedArtifact) -> None:
        source_node = self.add_source(source)
        self.upsert_node(
            artifact.id,
            artifact.type,
            artifact.relative_path,
            {
                "source_id": source.name,
                "hash": f"sha256:{artifact.sha256}",
                "path": str(artifact.path),
                "relative_path": artifact.relative_path,
                "size_bytes": artifact.size_bytes,
                "git_branch": artifact.git_branch,
                "git_commit": artifact.git_commit,
                "provenance": {"path": str(artifact.path), "relative_path": artifact.relative_path, "git_branch": artifact.git_branch, "git_commit": artifact.git_commit},
            },
        )
        self.upsert_edge(source_node, artifact.id, "indexes", {"source_id": source.name})
        for chunk in artifact.chunks:
            chunk_id = f"chunk:{source.name}:{artifact.relative_path}:sha256={chunk.text_hash}:chunk={chunk.index:04d}"
            self.upsert_node(
                chunk_id,
                "chunk",
                f"{artifact.relative_path}#{chunk.index}",
                {"source_id": source.name, "artifact_id": artifact.id, "start_line": chunk.start_line, "end_line": chunk.end_line, "text_hash": chunk.text_hash, "summary": chunk.text[:180], "token_estimate": chunk.token_estimate},
            )
            self.upsert_edge(artifact.id, chunk_id, "has_chunk", {"source_id": source.name})

    def remove_source_content(self, source_name: str) -> None:
        source_node = f"source:{self.kb_id}:{source_name}"
        nodes = [
            node_id
            for node_id, attrs in self.graph.nodes(data=True)
            if attrs.get("source_id") == source_name or node_id == source_node
        ]
        self.graph.remove_nodes_from(nodes)
