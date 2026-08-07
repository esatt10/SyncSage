from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from pathlib import Path

import yaml

from pheasant.config.loader import dump_config_yaml
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.ingestion.pipeline import utc_now
from pheasant.obsidian.exporter import ObsidianExporter
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.knowledge_base_registry import KnowledgeBaseRegistry
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.security.path_policy import resolve_config_write_target, resolve_under
from pheasant.sync.engine import SyncEngine


def apply_retrieval_criteria(
    results: list[dict],
    *,
    exclude_sources: list[str] | None = None,
    node_types: list[str] | None = None,
    min_score: float | None = None,
) -> list[dict]:
    """Filter search hits by caller-supplied criteria. Order is preserved.

    Ranking is not recomputed: these are *post-filters* over an already-ranked
    list, so a criterion can only ever remove rows. Re-scoring here would make
    the same query answer differently depending on which filters were passed,
    which is exactly the surprise a retrieval-tuning surface must not have.
    """
    excluded = {str(name) for name in (exclude_sources or [])}
    wanted_types = {str(name) for name in (node_types or [])}
    kept: list[dict] = []
    for item in results:
        if excluded and _source_of(item) in excluded:
            continue
        if wanted_types and str(item.get("type") or "") not in wanted_types:
            continue
        if min_score is not None:
            try:
                if float(item.get("score") or 0.0) < min_score:
                    continue
            except (TypeError, ValueError):
                continue
        kept.append(item)
    return kept


def _source_of(item: dict) -> str:
    """The source a search hit came from.

    Hits do **not** carry a top-level ``source_id`` — it lives under
    ``provenance``, alongside the path. Reading the wrong key here meant
    ``exclude_sources`` matched nothing and silently returned unfiltered
    results, which is worse than refusing the filter outright. Both shapes are
    accepted so a caller passing already-flattened rows still works.
    """
    provenance = item.get("provenance")
    if isinstance(provenance, dict) and provenance.get("source_id"):
        return str(provenance["source_id"])
    return str(item.get("source_id") or item.get("source") or "")


def _preview_rows(results: list[dict]) -> list[dict]:
    """Compact hits for a preview: enough to judge relevance, not the corpus."""
    rows = []
    for item in results:
        rows.append(
            {
                "id": item.get("id") or item.get("node_id"),
                "path": item.get("relative_path") or item.get("path"),
                "source_id": _source_of(item) or None,
                "type": item.get("type"),
                "score": item.get("score"),
                "preview": (str(item.get("text") or item.get("summary") or ""))[:200],
            }
        )
    return rows


class PheasantTools:
    def __init__(self, config: PheasantConfig):
        self.config = config
        self.paths = StatePaths.from_config(config)
        self.paths.ensure()
        self.state = StateStore(self.paths.sqlite)
        self.state.migrate()
        self.engine = SyncEngine(config, self.paths, self.state)
        self.searcher = HybridSearch(
            SearchStore(self.state),
            vector=self.engine.vector_searcher(),
            node_index=self.engine.node_index,
            wasm_relationship_search=config.search.wasm_relationship_search,
        )

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
                    self.config.pheasant.workspace_root,
                    self.config.pheasant.vault_path,
                    self.config.pheasant.exports_path,
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
        yaml_patch = dump_config_yaml({"sources": [source_payload]})
        wrote = False
        if write:
            if not config_path:
                raise ValueError("config_path is required when write=True")
            # An agent-supplied path must not become an arbitrary file write:
            # promotion may only touch this region's own config, or a path
            # under an allowed workspace root.
            path = resolve_config_write_target(
                config_path,
                server_config_path=os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml"),
                allowed_roots=[
                    self.config.pheasant.workspace_root,
                    *self.config.security.allow_workspace_roots,
                ],
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError(
                    f"Refusing to overwrite {path}: it is not a pheasant config mapping"
                )
            existing = [
                item for item in data.get("sources", []) or [] if item.get("name") != source.name
            ]
            data["sources"] = [*existing, source_payload]
            path.write_text(dump_config_yaml(data), encoding="utf-8")
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

    def scan_source(
        self,
        knowledge_base: str,
        source_name: str,
        max_depth: int | None = None,
    ) -> dict:
        """Estimate what a source would index, without indexing anything.

        Call this before syncing a source that points at something large or
        unfamiliar (a home directory, a whole drive). Reports the file count,
        total size, the largest subtrees, how many files each depth cap would
        admit, and whether the configured limits would refuse the sync.
        Reads no file content and writes nothing.
        """
        self._require_knowledge_base(knowledge_base)
        return self.engine.scan_source(source_name, max_depth=max_depth)

    def sync_source(
        self,
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        result = self.engine.sync_source(
            source_name,
            mode,  # type: ignore[arg-type]
            max_depth=max_depth,
            full_scan=full_scan,
        ).__dict__
        self._audit(source_name, "sync_source", "mcp", "mcp", None, utc_now(), result)
        return result

    def sync_all(
        self,
        knowledge_base: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        results = [
            r.__dict__
            for r in self.engine.sync_all(
                mode,  # type: ignore[arg-type]
                max_depth=max_depth,
                full_scan=full_scan,
            )
        ]
        for result in results:
            self._audit(result["source_id"], "sync_source", "mcp", "mcp", None, utc_now(), result)
        return {"results": results}

    def memory_write(
        self,
        knowledge_base: str,
        text: str,
        scope: str = "user",
        subject: str | None = None,
        supersedes: str | None = None,
        tags: list[str] | None = None,
        sync: bool = True,
    ) -> dict:
        """Append one memory record and (by default) index it immediately.

        The record lands as a Markdown file in the configured ``type: memory``
        source; ``sync=True`` runs an incremental sync of that source so the
        memory is retrievable via ``search_context`` in the same session
        (read-your-writes). Recall is ordinary search — no separate path.
        """
        from pheasant.memory.store import MemoryStore, memory_source

        self._require_knowledge_base(knowledge_base)
        source = memory_source(self.config)
        if source is None:
            raise ValueError(
                "no enabled memory source configured. Add one to pheasant.yaml:\n"
                "  sources:\n"
                "    - name: agent-memory\n"
                "      type: memory\n"
                "      path: memory"
            )
        record, created = MemoryStore(source.path).append(
            text, scope=scope, subject=subject, supersedes=supersedes, tags=tags or ()
        )
        result: dict = {"record": record.as_dict(), "created": created, "source": source.name}
        if sync and created:
            result["sync"] = self.engine.sync_source(source.name, "incremental").__dict__
        self._audit(
            source.name,
            "memory_write",
            "mcp",
            "mcp",
            None,
            utc_now(),
            {"record_id": record.record_id, "scope": record.scope, "created": created},
        )
        return result

    def memory_consolidate(self, knowledge_base: str) -> dict:
        """Run one memory-consolidation pass now (Step 33.2).

        Archives superseded and TTL-expired records (files renamed, never
        deleted) and re-syncs the memory source so they leave the index. The
        scheduler runs this automatically; this tool is the on-demand edge.
        """
        from pheasant.memory.maintenance import run_memory_maintenance

        self._require_knowledge_base(knowledge_base)
        result = run_memory_maintenance(self.engine)
        if result is None:
            return {
                "skipped": (
                    "memory consolidation is disabled or no `type: memory` source is configured"
                )
            }
        self._audit(
            result["source"], "memory_consolidate", "mcp", "mcp", None, utc_now(), result["report"]
        )
        return result

    def search_context(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        include_chunks: bool = True,
        include_graph_neighbors: bool = True,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
    ) -> dict:
        """Retrieve passages for a query.

        The last four parameters are **retrieval criteria** an agent can set
        per call rather than having them fixed in config: scope to one source
        or away from noisy ones, keep only certain node types, and floor the
        score. All are optional and default to the pre-existing behavior, so
        an existing caller is unaffected (CLAUDE.md §4 rule 8: additive only).
        """
        self._require_knowledge_base(knowledge_base)
        # Over-fetch when a filter will drop rows, so `max_results` still
        # means "give me this many" rather than "look at this many and give me
        # whatever survives" — the latter silently returns short answers.
        filtering = bool(exclude_sources or node_types or min_score is not None)
        fetch = max_results * 4 if filtering else max_results
        payload = self.searcher.search_context(
            knowledge_base or self.config.knowledge_base_id,
            query,
            mode,
            fetch,
            source_name,
            graph=self.engine.graph_builder.graph,
            principal=principal,
            principal_groups=principal_groups,
            security=self.config.security,
        )
        if filtering:
            payload = dict(payload)
            payload["results"] = apply_retrieval_criteria(
                payload.get("results") or [],
                exclude_sources=exclude_sources,
                node_types=node_types,
                min_score=min_score,
            )[:max_results]
            payload["criteria"] = {
                "source_name": source_name,
                "exclude_sources": exclude_sources,
                "node_types": node_types,
                "min_score": min_score,
            }
        return payload

    def describe_retrieval(self, knowledge_base: str) -> dict:
        """What this region's retrieval is configured to do, and what is tunable.

        An agent pointed at an unfamiliar region cannot otherwise tell whether
        semantic search exists, how many rounds the answering loop runs, or
        which sources are even present — so it either guesses or asks for
        everything. This returns the standing configuration, the criteria that
        may be overridden per call, and what the region can actually do
        (a vector mode is not offered when no vector index is built).
        """
        self._require_knowledge_base(knowledge_base)
        from pheasant.api.app import RETRIEVAL_FIELD_HELP

        settings = self.config.assistant.retrieval
        has_vectors = self.engine.vectors is not None
        modes = ["text", "graph", "hybrid"] + (["vector"] if has_vectors else [])
        rows = self.state.rows("SELECT DISTINCT source_id FROM artifacts ORDER BY source_id")
        return {
            "knowledge_base": knowledge_base or self.config.knowledge_base_id,
            "default_mode": self.config.search.default_mode,
            "max_results_default": self.config.search.max_results_default,
            "available_modes": modes,
            "vector_index": {
                "enabled": self.config.search.embeddings.enabled,
                "built": has_vectors,
                "provider": self.config.search.embeddings.provider,
            },
            "sources": [str(row["source_id"]) for row in rows],
            "retrieval": settings.model_dump(mode="json"),
            "workflow": self.config.assistant.workflow,
            "workflow_options": dict(self.config.assistant.workflow_options or {}),
            "tunable_per_call": {
                "search_context": [
                    "mode",
                    "max_results",
                    "source_name",
                    "exclude_sources",
                    "node_types",
                    "min_score",
                ],
                "ask_knowledge_base": ["workflow", "mode", "max_results", "options"],
            },
            "field_help": RETRIEVAL_FIELD_HELP,
        }

    def preview_retrieval(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
    ) -> dict:
        """Run retrieval criteria and report how they differ from the config.

        The point is to make a configuration **testable before it is
        committed**: the same criteria that would go into
        ``assistant.retrieval`` / ``search.default_mode`` can be tried here,
        against the region's own content, and compared with what the standing
        configuration returns for the same query. Returns both result sets in
        compact form plus the delta — what the criteria added, dropped and
        kept — so an agent can decide rather than guess.

        Read-only: nothing is persisted and the live config is untouched.
        """
        self._require_knowledge_base(knowledge_base)
        kb = knowledge_base or self.config.knowledge_base_id

        baseline = self.searcher.search_context(
            kb,
            query,
            self.config.search.default_mode,
            self.config.search.max_results_default,
            graph=self.engine.graph_builder.graph,
            security=self.config.security,
        )
        candidate = self.search_context(
            kb,
            query,
            mode,
            max_results,
            source_name=source_name,
            exclude_sources=exclude_sources,
            node_types=node_types,
            min_score=min_score,
        )

        def keys(payload: dict) -> list[str]:
            out = []
            for item in payload.get("results") or []:
                out.append(str(item.get("id") or item.get("node_id") or item.get("path") or ""))
            return [key for key in out if key]

        baseline_keys = keys(baseline)
        candidate_keys = keys(candidate)
        baseline_set, candidate_set = set(baseline_keys), set(candidate_keys)
        return {
            "query": query,
            "criteria": {
                "mode": mode,
                "max_results": max_results,
                "source_name": source_name,
                "exclude_sources": exclude_sources,
                "node_types": node_types,
                "min_score": min_score,
            },
            "configured": {
                "mode": self.config.search.default_mode,
                "max_results": self.config.search.max_results_default,
            },
            "results": _preview_rows(candidate.get("results") or []),
            "baseline_results": _preview_rows(baseline.get("results") or []),
            "delta": {
                "added": [key for key in candidate_keys if key not in baseline_set],
                "dropped": [key for key in baseline_keys if key not in candidate_set],
                "kept": [key for key in candidate_keys if key in baseline_set],
                "result_count": len(candidate_keys),
                "baseline_count": len(baseline_keys),
            },
            "persisted": False,
            "how_to_apply": (
                "PUT /assistant/retrieval, or set search.default_mode / "
                "assistant.retrieval in pheasant.yaml (`pheasant setup --advanced`)."
            ),
        }

    def ask_knowledge_base(
        self,
        knowledge_base: str,
        question: str,
        workflow: str | None = None,
        mode: str = "hybrid",
        max_results: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
    ) -> dict:
        """Answer a question from the knowledge base, with citations and graph facts.

        Runs the configured question-answering workflow — by default the
        LangGraph agent when the ``[agent]`` extra is installed and a model
        is reachable, otherwise a single retrieval pass. Prefer this over
        ``search_context`` when you want a synthesized answer rather than
        raw passages to reason over yourself; the returned ``steps`` show
        what the agent actually did.

        With no model configured the answer is extractive (the retrieved
        passages, attributed), so this is always safe to call.
        """
        from pheasant.assistant.chat import answer_question

        self._require_knowledge_base(knowledge_base)
        return answer_question(
            question,
            search=self.searcher,
            knowledge_base=knowledge_base or self.config.knowledge_base_id,
            config=self.config,
            graph=self.engine.graph_builder.graph,
            state=self.state,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
            workflow=workflow,
            options=options,
        )

    def get_relevant_files(
        self,
        knowledge_base: str,
        task: str,
        source_name: str | None = None,
        max_files: int = 8,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        # This is `search_context` with a different projection, so it runs
        # under the same ACL enforcement — without `security` it returned
        # unfiltered hits to every caller whenever acl_enforced was on.
        # No `graph=`: this route answers with files, and graph nodes carry
        # no path, so admitting them would crowd the file hits out.
        payload = self.searcher.search_context(
            knowledge_base or self.config.knowledge_base_id,
            task,
            "hybrid",
            max_files,
            source_name,
            principal=principal,
            principal_groups=principal_groups,
            security=self.config.security,
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
                    data for data in edge_map.values() if not allowed or data.get("type") in allowed
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
                        "node": dict(graph.nodes.get(target, {})),
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
        attrs = graph.nodes.get(node_id)
        if attrs is None:
            return {"node_id": node_id, "explanation": "Node is not present in the current graph."}
        node = dict(attrs)
        return {
            "node_id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "explanation": f"{node.get('label')} is a {node.get('type')} node indexed by pheasant.",
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

    def get_contract(self, knowledge_base: str) -> dict:
        """Return this region's published semantic contract (Synapse 21.5).

        ``{"status": "unpublished"}`` until a contract has been published.
        """
        self._require_knowledge_base(knowledge_base)
        import json as _json

        from pheasant.synapse.publisher import load_contract_text

        text = load_contract_text(self.config.pheasant.state_path)
        if text is None:
            return {"status": "unpublished", "knowledge_base": self.config.knowledge_base_id}
        return _json.loads(text)

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
            "nodes": [
                dict(attrs) for attrs in (graph.nodes.get(item) for item in node_ids) if attrs
            ],
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
        return PheasantConfig.model_validate({"sources": [payload]}).sources[0]

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
