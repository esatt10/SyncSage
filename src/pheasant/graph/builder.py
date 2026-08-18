from __future__ import annotations

import hashlib
import logging
from typing import Any

from pheasant.config.schema import PheasantConfig, SourceConfig
from pheasant.graph.enrichment import (
    ArtifactEnrichment,
    CodeEnrichmentPass,
    MarkdownDocumentEnrichmentPass,
    SemanticSimilarityPass,
    resolve_cross_source_edges,
)
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.ingestion.content_types import ARTIFACT_TYPES
from pheasant.ingestion.pipeline import ParsedArtifact, utc_now
from pheasant.sync.pacing import serve_yield

logger = logging.getLogger(__name__)


class GraphBuilder:
    def __init__(self, config: PheasantConfig):
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
            self.graph.nodes[node_id].get("created_at") if self.graph.has_node(node_id) else now
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
        source_type = source.type.value
        self.upsert_node(
            node_id,
            "source",
            source.name,
            {
                "source_id": source.name,
                "source_type": source_type,
                "path": str(source.path),
            },
        )
        self.upsert_edge(self.kb_id, node_id, "contains", {"source_id": source.name})
        self.add_source_type(source_type, node_id, source.name)
        return node_id

    def add_source_type(self, source_type: str, source_node: str, source_id: str) -> str:
        """A hub node grouping every source of one kind.

        The type was already an *attribute* of each source node, which meant it
        could be read but never navigated: nothing in the graph connected the
        two Confluence spaces to each other, so "show me everything that came
        out of a wiki" was a question the picture could not answer. A hub makes
        that a visible structure — one node, one hop from each source of that
        kind — which is what the graph is for.

        Hung off the knowledge base **alongside** sources rather than between
        them: `kb contains source` stays exactly as it was, so a source is
        still one hop from the root and nothing already on screen at the
        default depth gets pushed past the horizon. The hub is a sibling, not
        a new tier.

        `contains` rather than a new edge type, because it is the same
        structural grouping the graph already uses for kb→source,
        directory→file and section→subsection — and because the assistant's
        semantic walk deliberately skips the structural edges, which is right:
        a type hub carries no content to retrieve.
        """

        node_id = f"source_type:{self.kb_id}:{source_type}"
        self.upsert_node(
            node_id,
            "source_type",
            source_type,
            {"source_type": source_type},
        )
        self.upsert_edge(self.kb_id, node_id, "contains", {"source_type": source_type})
        self.upsert_edge(node_id, source_node, "contains", {"source_id": source_id})
        return node_id

    def add_directory_chain(
        self, source: SourceConfig, relative_path: str, branch: str | None
    ) -> str:
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
        self.add_headings(source, artifact)
        self.apply_enrichment(enrichment)
        return enrichment

    def add_memory_edges(self, state: Any, max_targets: int | None = None) -> dict[str, Any]:
        """Wire memory records into the graph (Step 33.7). Returns a report.

        Two kinds of edge, both previously missing:

        * ``supersedes`` — a documented edge type nothing emitted, so a
          correction existed only as a frontmatter string and the graph could
          not answer "what replaced this".
        * ``about`` — the record to what it is *about*, by the strongest signal
          that fires (see :mod:`pheasant.memory.bridge`).

        A no-op returning zeros when the region has no memory records, so a
        graph without agent memory is byte-identical to before. Idempotent via
        ``upsert_edge``: re-running over unchanged content re-derives the same
        edges and changes nothing.
        """
        from pheasant.memory.bridge import (
            ABOUT_EDGE,
            DEFAULT_MAX_TARGETS,
            resolve_bridges,
            supersedes_edges,
        )

        report = {"records": 0, "about": 0, "supersedes": 0, "unbridged": [], "by_signal": {}}
        records = self._memory_rows(state)
        if not records:
            return report
        report["records"] = len(records)

        for newer, older in supersedes_edges(records):
            self.upsert_edge(newer, older, "supersedes", {"enrichment_pass": "memory_bridge"})
            report["supersedes"] += 1

        inputs = self._bridge_inputs(state, records)
        edges, unbridged = resolve_bridges(
            inputs, max_targets if max_targets is not None else DEFAULT_MAX_TARGETS
        )
        for edge in edges:
            self.upsert_edge(
                edge.artifact_id,
                edge.target_id,
                ABOUT_EDGE,
                {
                    "record_id": edge.record_id,
                    "match_signal": edge.signal,
                    "matched": edge.matched,
                    "confidence": edge.confidence,
                    "enrichment_pass": "memory_bridge",
                },
            )
            report["by_signal"][edge.signal] = report["by_signal"].get(edge.signal, 0) + 1
        report["about"] = len(edges)
        # Reported, not silent: a corpus where no rung fires is a real outcome
        # an operator should be able to see, not a feature that quietly does
        # nothing. Surfaced through describe_retrieval's memory block.
        report["unbridged"] = unbridged
        return report

    @staticmethod
    def _memory_rows(state: Any) -> list[dict[str, Any]]:
        try:
            rows = state.rows(
                "SELECT record_id, artifact_id, source_id, supersedes FROM memory_records"
            )
        except Exception:  # pragma: no cover - state store older than 33.5
            return []
        return [dict(row) for row in rows]

    def _bridge_inputs(self, state: Any, records: list[dict[str, Any]]) -> Any:
        """Read the ladder's inputs out of SQLite and the graph.

        Corpus-only on purpose: a memory must not be matched against symbols,
        headings or entities extracted from *another memory*, or a store of
        related notes would knit itself together and call that grounding.
        """
        from pheasant.memory.bridge import BridgeInputs, normalize_label

        memory_sources = {str(row.get("source_id") or "") for row in records}
        memory_artifacts = {str(row.get("artifact_id") or "") for row in records}

        texts: dict[str, str] = {}
        placeholders = ",".join("?" for _ in memory_sources)
        for row in state.rows(
            "SELECT artifact_id, GROUP_CONCAT(text, ' ') AS body FROM chunks "
            f"WHERE source_id IN ({placeholders}) GROUP BY artifact_id",
            tuple(memory_sources),
        ):
            texts[str(row["artifact_id"])] = str(row["body"] or "")

        symbols: dict[str, list[str]] = {}
        for row in state.rows(
            "SELECT name, qualified_name, artifact_id FROM symbols "
            f"WHERE source_id NOT IN ({placeholders})",
            tuple(memory_sources),
        ):
            for value in (row["name"], row["qualified_name"]):
                if value:
                    symbols.setdefault(str(value).lower(), []).append(str(row["artifact_id"]))

        headings: dict[str, list[str]] = {}
        entities: dict[str, list[str]] = {}
        referenced: dict[str, list[str]] = {}
        node_types: dict[str, str] = {}
        with self.graph.reading():
            for node_id, attrs in self.graph.iter_nodes():
                node_types[node_id] = str(attrs.get("type") or "")
                node_type = attrs.get("type")
                if attrs.get("source_id") in memory_sources:
                    continue
                if node_type == "heading":
                    key = normalize_label(attrs.get("title") or attrs.get("label") or "")
                    if key:
                        headings.setdefault(key, []).append(node_id)
                elif node_type == "entity":
                    key = normalize_label(attrs.get("label") or "")
                    if key:
                        entities.setdefault(key, []).append(node_id)
            for (source, target), edge_map in self.graph.iter_edges():
                # Rung 1: an explicit link the cross-source pass already
                # resolved to a real artifact. One pair can carry several edge
                # types, so the map is what has to be inspected.
                if source not in memory_artifacts:
                    continue
                for data in edge_map.values():
                    if data.get("type") not in {"references", "imports"}:
                        continue
                    # Only the *resolved* artifact, not the `external_reference`
                    # stub the link itself produced. Both edges exist and both
                    # are `references`, so taking either spent half the
                    # per-record cap pointing at a node that stands for the
                    # link rather than for what the record is about — visible
                    # on a live run, where one record drew two edges and only
                    # one of them reached a file.
                    if node_types.get(target) in ARTIFACT_TYPES:
                        referenced.setdefault(source, []).append(target)
                    break

        return BridgeInputs(
            records=records,
            texts=texts,
            symbols={key: sorted(set(value)) for key, value in symbols.items()},
            headings={key: sorted(set(value)) for key, value in headings.items()},
            entities={key: sorted(set(value)) for key, value in entities.items()},
            referenced={key: sorted(set(value)) for key, value in referenced.items()},
        )

    def add_headings(self, source: SourceConfig, artifact: ParsedArtifact) -> int:
        """Emit the artifact's structural outline as `heading` nodes.

        `heading` and `has_heading` are both **documented** in
        `docs/graph_model.md` ("Retrieval and document structure units" /
        "Hierarchy and indexing relationships") and were never emitted
        anywhere — this connects them. Nothing happens unless the source
        enables `taxonomy` (and `taxonomy.graph_nodes`), so a graph without
        the feature is byte-identical to before.

        Two edge types, both existing: the artifact `has_heading` each of its
        sections, and a section `contains` its subsections — the same edge the
        directory/file hierarchy uses, so the taxonomy is walkable by the
        graph traversal that already knows how to follow `contains` (see
        `api/app.py:HIERARCHY_EDGE_TYPES`).
        """
        headings = getattr(artifact, "headings", None)
        if not headings:
            return 0
        if not getattr(getattr(source, "taxonomy", None), "graph_nodes", True):
            return 0

        # Stack of (level, node_id) so a subsection is parented to the section
        # that encloses it rather than to the artifact.
        stack: list[tuple[int, str]] = []
        emitted = 0
        for heading in headings:
            # Keyed on the section's *breadcrumb*, hashed — deliberately not on
            # its line number. A line-numbered ID churns the whole graph on any
            # edit: inserting one paragraph shifts every heading below it, so
            # every section downstream would be dropped and re-created despite
            # nothing about it changing. The breadcrumb is the section's
            # identity ("Article IV > 4.2 Termination" is that section wherever
            # it sits), and hashing keeps the ID bounded when a deep path runs
            # to hundreds of characters. Matches the `chunk:` ID's existing use
            # of a content hash in the context field.
            #
            # Trade-off, accepted: two sections with a byte-identical breadcrumb
            # in one document collapse to one node. That needs a document to
            # repeat the same caption under the same parent, and when it happens
            # the two really are the same section by name — whereas edit churn
            # would be constant.
            digest = hashlib.sha256(heading.path.encode("utf-8")).hexdigest()[:16]
            heading_id = f"heading:{source.name}:{artifact.relative_path}:sha256={digest}"
            self.upsert_node(
                heading_id,
                "heading",
                heading.label,
                {
                    "source_id": source.name,
                    "artifact_id": artifact.id,
                    "level": heading.level,
                    "pattern_level": heading.pattern_level,
                    "number": heading.number,
                    "title": heading.title,
                    "kind": heading.kind,
                    "heading_path": heading.path,
                    "start_line": heading.line,
                    "relative_path": artifact.relative_path,
                    # The parsed ordinal, persisted so a reader can query by
                    # citation ("§ 12.3" -> parts [12, 3]) and so the taxonomy
                    # endpoint can re-derive sequence issues without reparsing
                    # the document.
                    "ordinal_parts": list(heading.ordinal.parts) if heading.ordinal else [],
                    "ordinal_series": heading.ordinal.series if heading.ordinal else None,
                    "ordinal_suffix": heading.ordinal.suffix if heading.ordinal else "",
                    "ordinal_relative": bool(heading.ordinal.relative)
                    if heading.ordinal
                    else False,
                },
            )
            while stack and stack[-1][0] >= heading.level:
                stack.pop()
            parent = stack[-1][1] if stack else None
            if parent is None:
                self.upsert_edge(artifact.id, heading_id, "has_heading", {"source_id": source.name})
            else:
                self.upsert_edge(parent, heading_id, "contains", {"source_id": source.name})
                # Also link the artifact to every section, not just the roots,
                # so "which sections does this document have?" is one hop
                # rather than a full descent.
                self.upsert_edge(artifact.id, heading_id, "has_heading", {"source_id": source.name})
            stack.append((heading.level, heading_id))
            emitted += 1
        return emitted

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
                if attrs.get("type") not in ARTIFACT_TYPES:
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
        resolved = self._resolve_cross_source_edges(nodes, ref_edges)
        for index, edge in enumerate(resolved):
            self.upsert_edge(edge.source, edge.target, edge.type, dict(edge.attrs))
            if index % 500 == 499:
                serve_yield()
        return len(resolved)

    def _resolve_cross_source_edges(
        self,
        nodes: list[tuple[str, dict[str, Any]]],
        ref_edges: list[tuple[str, str, str, str | None]],
    ) -> list[Any]:
        """Synapse Step 34.5a: optional WASM acceleration, pure-Python default.

        Opt-in via ``graph.wasm_cross_source_resolution`` (default off — see
        the 34.4 benchmark spike for why this one is conditional rather than
        a clear win). Any failure — the ``[wasm]`` extra missing, a sandbox
        error, anything — falls back to the pure-Python function rather than
        failing the sync; acceleration is a performance path, never a
        correctness dependency.
        """
        if not self.config.graph.wasm_cross_source_resolution:
            return resolve_cross_source_edges(nodes, ref_edges)
        try:
            from pheasant.sandbox.accel import resolve_cross_source_edges_wasm

            return resolve_cross_source_edges_wasm(nodes, ref_edges)
        except Exception:
            logger.warning(
                "WASM cross-source resolution failed; falling back to pure Python", exc_info=True
            )
            return resolve_cross_source_edges(nodes, ref_edges)

    def remove_source_content(self, source_name: str) -> None:
        source_node = f"source:{self.kb_id}:{source_name}"
        nodes = [
            node_id
            for node_id, attrs in self.graph.nodes(data=True)
            if attrs.get("source_id") == source_name or node_id == source_node
        ]
        self.graph.remove_nodes_from(nodes)


def _batches(items: list[str], size: int):
    """Chunk ids so an IN (...) clause stays inside SQLite's parameter limit."""

    for start in range(0, len(items), size):
        yield items[start : start + size]
