from __future__ import annotations

from syncsage.config.schema import SourceConfig, SyncSageConfig
from syncsage.graph.enrichment import (
    ArtifactEnrichment,
    CodeEnrichmentPass,
    MarkdownDocumentEnrichmentPass,
    SemanticSimilarityPass,
    resolve_cross_source_edges,
)
from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.ingestion.pipeline import ParsedArtifact, utc_now
from syncsage.sync.pacing import serve_yield


class GraphBuilder:
    def __init__(self, config: SyncSageConfig):
        self.config = config
        self.graph = SimpleMultiDiGraph()
        self.kb_id = config.knowledge_base_id
        self.upsert_node(self.kb_id, "knowledge_base", self.kb_id, {})
        self.enrichment_passes = [
            CodeEnrichmentPass(),
            MarkdownDocumentEnrichmentPass(),
        ]
        self.similarity_pass = SemanticSimilarityPass()

    def upsert_node(self, node_id: str, node_type: str, label: str, attrs: dict) -> None:
        now = utc_now()
        existing_created = (
            self.graph.nodes[node_id].get("created_at")
            if self.graph.has_node(node_id)
            else now
        )
        self.graph.add_node(
            node_id,
            id=node_id,
            type=node_type,
            label=label,
            created_at=existing_created,
            updated_at=now,
            knowledge_base_id=self.kb_id,
            **attrs,
        )

    def upsert_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        attrs: dict | None = None,
    ) -> None:
        attrs = attrs or {}
        for _key, data in self.graph.get_edge_data(source, target, default={}).items():
            if data.get("type") == edge_type:
                data.update(attrs)
                data["updated_at"] = utc_now()
                return
        self.graph.add_edge(
            source,
            target,
            type=edge_type,
            created_at=utc_now(),
            confidence=attrs.pop("confidence", 1.0),
            **attrs,
        )

    def add_source(self, source: SourceConfig) -> str:
        node_id = f"source:{self.kb_id}:{source.name}"
        self.upsert_node(
            node_id,
            "source",
            source.name,
            {
                "source_id": source.name,
                "source_type": source.type.value,
                "path": str(source.path),
            },
        )
        self.upsert_edge(self.kb_id, node_id, "contains", {"source_id": source.name})
        return node_id

    def add_directory_chain(self, source: SourceConfig, relative_path: str, branch: str | None) -> str:
        source_node = self.add_source(source)
        parent = source_node
        parts = [part for part in relative_path.replace("\\", "/").split("/")[:-1] if part]
        prefix: list[str] = []
        for depth, part in enumerate(parts, start=1):
            prefix.append(part)
            relative = "/".join(prefix)
            node_id = f"directory:{source.name}:{relative}:branch={branch or 'none'}"
            self.upsert_node(
                node_id,
                "directory",
                part,
                {
                    "source_id": source.name,
                    "relative_path": relative,
                    "path": str(source.path / relative),
                    "depth": depth,
                    "provenance": {
                        "path": str(source.path / relative),
                        "relative_path": relative,
                        "git_branch": branch,
                    },
                },
            )
            self.upsert_edge(parent, node_id, "contains", {"source_id": source.name})
            parent = node_id
        return parent

    def add_artifact(
        self,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        source_node = self.add_source(source)
        parent_node = self.add_directory_chain(source, artifact.relative_path, artifact.git_branch)
        enrichment = self.enrich_artifact(source, artifact)
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
                "concept_terms": sorted(enrichment.concept_terms),
                "provenance": {
                    "path": str(artifact.path),
                    "relative_path": artifact.relative_path,
                    "git_branch": artifact.git_branch,
                    "git_commit": artifact.git_commit,
                },
            },
        )
        self.upsert_edge(source_node, artifact.id, "indexes", {"source_id": source.name})
        self.upsert_edge(parent_node, artifact.id, "contains", {"source_id": source.name})
        for chunk in artifact.chunks:
            chunk_id = (
                f"chunk:{source.name}:{artifact.relative_path}:"
                f"sha256={chunk.text_hash}:chunk={chunk.index:04d}"
            )
            self.upsert_node(
                chunk_id,
                "chunk",
                f"{artifact.relative_path}#{chunk.index}",
                {
                    "source_id": source.name,
                    "artifact_id": artifact.id,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "text_hash": chunk.text_hash,
                    "summary": chunk.text[:180],
                    "token_estimate": chunk.token_estimate,
                },
            )
            self.upsert_edge(artifact.id, chunk_id, "has_chunk", {"source_id": source.name})
        self.apply_enrichment(enrichment)
        return enrichment

    def enrich_artifact(
        self,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        enrichment = ArtifactEnrichment()
        for enrichment_pass in self.enrichment_passes:
            enrichment.extend(enrichment_pass.run(self.kb_id, source, artifact))
        return enrichment

    def apply_enrichment(self, enrichment: ArtifactEnrichment) -> None:
        for node in enrichment.nodes:
            self.upsert_node(node.id, node.type, node.label, node.attrs)
        for edge in enrichment.edges:
            self.upsert_edge(edge.source, edge.target, edge.type, edge.attrs)

    def add_similarity_edges(
        self,
        source_name: str | None = None,
        changed_ids: set[str] | None = None,
    ) -> None:
        """Link artifacts that share concept terms.

        ``changed_ids`` restricts the pass to pairs touching an artifact this
        sync actually rewrote; the rest already have their edges and are
        idempotent, so re-deriving them only costs CPU.
        """

        artifacts = []
        with self.graph.reading():
            for node_id, attrs in self.graph.iter_nodes():
                if attrs.get("type") not in {"file", "markdown_note", "document"}:
                    continue
                if source_name and attrs.get("source_id") != source_name:
                    continue
                artifacts.append((node_id, dict(attrs)))
        for index, edge in enumerate(self.similarity_pass.run(artifacts, changed_ids=changed_ids)):
            self.upsert_edge(edge.source, edge.target, edge.type, edge.attrs)
            # Enrichment is one long CPU-bound stretch; without a yield it
            # blocks every request thread for its whole duration.
            if index % 500 == 499:
                serve_yield()

    def add_cross_source_edges(self) -> int:
        """Resolve references whose targets resolve into a *different* source.

        Synapse 21.6B. A global post-pass over the whole graph (sources sync
        independently, so a reference can only resolve once both the
        referencing and the target source are indexed). Python imports resolve
        ``imports`` edges; markdown/document links resolve ``references`` edges.
        Edges are upserted, so re-running is idempotent and deterministic.
        Returns the number of cross-source edges resolved this pass.
        """

        ref_edges: list[tuple[str, str, str, str | None]] = []
        # One lock hold, no copying: this walks every edge in the graph, so
        # snapshotting them first (1.5M dict copies on a real index) cost more
        # than the pass itself.
        with self.graph.reading():
            nodes = [(node_id, dict(attrs)) for node_id, attrs in self.graph.iter_nodes()]
            node_map = self.graph.node_map()
            for (source, target), edge_map in self.graph.iter_edges():
                target_attrs = node_map.get(target)
                if not target_attrs or target_attrs.get("type") != "external_reference":
                    continue
                for data in edge_map.values():
                    edge_type = data.get("type")
                    if edge_type not in {"imports", "references"}:
                        continue
                    ref_edges.append((source, target, edge_type, data.get("reference_type")))
        resolved = resolve_cross_source_edges(nodes, ref_edges)
        for index, edge in enumerate(resolved):
            self.upsert_edge(edge.source, edge.target, edge.type, dict(edge.attrs))
            if index % 500 == 499:
                serve_yield()
        return len(resolved)

    def remove_source_content(self, source_name: str) -> None:
        source_node = f"source:{self.kb_id}:{source_name}"
        nodes = [
            node_id
            for node_id, attrs in self.graph.nodes(data=True)
            if attrs.get("source_id") == source_name or node_id == source_node
        ]
        self.graph.remove_nodes_from(nodes)
