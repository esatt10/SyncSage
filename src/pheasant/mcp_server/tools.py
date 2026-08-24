from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from pheasant.config.loader import dump_config_yaml
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.ingestion.pipeline import utc_now
from pheasant.jobs import JobRegistry
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.knowledge_base_registry import KnowledgeBaseRegistry
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.criteria import (
    apply_retrieval_criteria,
    criteria_active,
    criteria_dict,
    source_of,
)
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.security.path_policy import (
    configured_roots,
    resolve_config_write_target,
    resolve_under,
)
from pheasant.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

#: Step 33.6 — the criteria filters moved to `pheasant.search.criteria` so
#: `POST /search` and the MCP tool share one implementation instead of HTTP
#: silently having fewer filters. `apply_retrieval_criteria` stays importable
#: from this module (it is imported above, and callers and tests reach for it
#: here); `_source_of` keeps its old private name for the same reason.
_source_of = source_of


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
        self.state = StateStore.from_config(config, self.paths.sqlite)
        self.state.migrate()
        self.engine = SyncEngine(config, self.paths, self.state)
        # MCP and A2A callers cannot consume the HTTP server's in-process job
        # registry.  Keep the same progress contract on the tool facade so an
        # agent can start a long first index, retain the job id, and inspect it
        # without holding a tool call open for minutes.
        self.jobs = JobRegistry()
        self._job_lock = threading.Lock()
        self.searcher = HybridSearch(
            SearchStore(self.state),
            vector=self.engine.vector_searcher(),
            node_index=self.engine.node_index,
            wasm_relationship_search=config.search.wasm_relationship_search,
            steering_enabled=config.memory.steering_enabled,
            default_memory_policy=config.memory.default_policy,
            usage_tracking=config.memory.usage_tracking,
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
        taxonomy: bool = False,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
        sync_now: bool = False,
        wait: bool = False,
        sync_mode: str = "incremental",
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        if self.config.security.allow_user_selected_source_paths:
            resolved_path = Path(path).expanduser().resolve()
            if not resolved_path.exists():
                raise ValueError(f"Path does not exist: {resolved_path}")
        else:
            # Security audit finding M4: shared with the HTTP register/promote
            # routes (`api/app.py`'s `_configured_roots`) via
            # `security.path_policy.configured_roots`, so this allow-list
            # cannot drift out of agreement with theirs — and, unlike the
            # roots this call site built inline before, does not drop a
            # root that is not currently mounted (`resolve_under` now
            # refuses an empty list rather than treating it as "unrestricted").
            resolved_path = resolve_under(path, configured_roots(self.config))
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
        if taxonomy:
            # Structural taxonomy extraction (chapters/sections/§ codes) for
            # books, procedures and legal documents. Additive optional
            # parameter: an agent that never passes it registers exactly the
            # source it did before (rule 8 — additive evolution only).
            source.taxonomy.enabled = True
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
        response = {
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
        if sync_now:
            if wait:
                response["sync_result"] = self.sync_source(knowledge_base, source.name, sync_mode)
            else:
                response["job"] = self.start_sync_source(knowledge_base, source.name, sync_mode)
        return response

    def start_sync_source(
        self,
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Start indexing and immediately return a followable job record."""
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        with self._job_lock:
            active = self.jobs.active_for(source_name)
            if active is not None:
                return active.as_dict()
            job = self.jobs.create("sync", f"Indexing {source_name}", [source_name])

        def run() -> None:
            def progress(phase: str, current: int, total: int | None, detail: str) -> None:
                self.jobs.progress(
                    job.id,
                    phase=phase,
                    current=current,
                    total=total,
                    detail=detail,
                )

            try:
                result = self.engine.sync_source(
                    source_name,
                    mode,  # type: ignore[arg-type]
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=progress,
                ).__dict__
            except Exception as exc:  # background tool work cannot propagate
                self.jobs.finish(job.id, "failed", error=str(exc))
                return
            failed = result.get("status") in {"failed", "timeout", "limit_exceeded"}
            self.jobs.finish(
                job.id,
                "failed" if failed else "succeeded",
                error=(result.get("details") or {}).get("error") if failed else None,
                result=result,
            )

        threading.Thread(
            target=run,
            name=f"pheasant-mcp-sync-{source_name}",
            daemon=True,
        ).start()
        return job.as_dict()

    def get_job(self, job_id: str) -> dict:
        """Return current progress and the terminal result for one tool job."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        return job

    def list_jobs(self, active_only: bool = False, limit: int = 50) -> dict:
        """List recent tool jobs, with active indexing jobs first."""
        return {"jobs": self.jobs.list(active_only=active_only, limit=limit)}

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
        kind: str = "fact",
        principal: str | None = None,
        valid_until: str | None = None,
    ) -> dict:
        """Append one memory record and (by default) index it immediately.

        The record lands as a Markdown file in the configured ``type: memory``
        source; ``sync=True`` runs an incremental sync of that source so the
        memory is retrievable via ``search_context`` in the same session
        (read-your-writes). Recall is ordinary search — no separate path.

        ``principal`` records *who asserted this* — it is what lets a later
        step scope a user's memories to that user rather than to everyone
        (Step 33.11), and it is part of the record id, so two agents writing
        the same sentence in the same second get two records instead of
        silently sharing one. ``kind`` marks a record as retrieval policy
        rather than a fact; ``valid_until`` gives it an expiry it declares
        itself. All three are optional and default to the pre-33.5 behavior
        (CLAUDE.md §4 rule 8: additive only).
        """
        from pheasant.memory.policy import STEERING_KINDS
        from pheasant.memory.reinforcement import StateReinforcementIndex
        from pheasant.memory.store import MemoryStore, memory_source

        self._require_knowledge_base(knowledge_base)
        # Security audit finding H3: see the matching comment on HTTP
        # POST /memory. True by default (rule 7 — unchanged for the
        # single-user case).
        if not self.config.memory.allow_org_scope_writes and (
            scope == "org" or kind in STEERING_KINDS
        ):
            raise ValueError(
                "memory.allow_org_scope_writes is false: org-scope and steering-kind "
                "records may not be written over MCP. Write session/user scope instead, "
                "or enable memory.allow_org_scope_writes."
            )
        source = memory_source(self.config, self.state)
        if source is None:
            raise ValueError(
                "no enabled memory source configured. Add one to pheasant.yaml:\n"
                "  sources:\n"
                "    - name: agent-memory\n"
                "      type: memory\n"
                "      path: memory"
            )
        # Phase 1: a write whose *normalized* text already matches a live
        # record in the same (scope, subject, kind, ACL) bucket reinforces
        # that record instead of creating a new one. `reinforcement_enabled`
        # defaults on (see MemorySettings) but stays a real knob, and a
        # cold/absent projection degrades `find()` to "no match" — never
        # blocks the write.
        reinforcement = (
            StateReinforcementIndex(self.state)
            if self.config.memory.reinforcement_enabled
            else None
        )
        store = MemoryStore(source.path)
        record, created = store.append(
            text,
            scope=scope,
            subject=subject,
            supersedes=supersedes,
            tags=tags or (),
            kind=kind,
            written_by=principal,
            valid_until=valid_until,
            reinforcement=reinforcement,
        )
        outcome = store.last_outcome or ("created" if created else "duplicate")

        from pheasant.telemetry import metrics as _metrics

        _metrics.record_memory_write(outcome, store.last_fold)
        result: dict = {
            "record": record.as_dict(),
            "created": created,
            "source": source.name,
            # Additive (rule 8): "created" | "reinforced" | "duplicate".
            # `created` is unchanged and still means exactly what it always
            # has; `outcome` distinguishes the two ways `created=False` can
            # now happen.
            "outcome": outcome,
        }
        submitted = (text or "").strip()
        if not created and submitted and submitted != record.text:
            # Only possible for outcome="reinforced": an exact-digest match
            # (outcome="duplicate") is byte-identical to `record.text` by
            # construction. The caller gets back a record whose stored text
            # is not what it wrote — worth surfacing rather than leaving
            # implicit.
            result["submitted_text"] = submitted
        # The record is already durably on disk; this sync only makes it
        # *searchable now*. Failing the whole request when it cannot run — most
        # often because another writer holds the engine lease, which a live run
        # hit immediately — reports "your memory was not saved" when it was.
        # Degrade to deferred indexing instead: the scheduler and the next sync
        # both pick it up.
        if sync and created:
            try:
                # `enrich="deferred"`: a write must not pay the whole-graph
                # walk (similarity + cross-source + memory bridge edges) —
                # see sync/engine.py:_finalize_index_state. The next
                # scheduler beat runs it and clears the dirty flag.
                result["sync"] = self.engine.sync_source(
                    source.name, "incremental", enrich="deferred"
                ).__dict__
            except Exception as exc:
                logger.warning("memory write indexed later: %s", exc)
                result["sync_deferred"] = str(exc)
        self._audit(
            source.name,
            "memory_write",
            principal or "mcp",
            "mcp",
            None,
            utc_now(),
            {
                "record_id": record.record_id,
                "scope": record.scope,
                "kind": record.kind,
                "created": created,
            },
        )
        return result

    def memory_list(
        self,
        knowledge_base: str,
        scope: str | None = None,
        current_only: bool = True,
        principal: str | None = None,
    ) -> dict:
        """This region's memory records, newest last.

        Backs the `pheasant://…/memory` resource (which never passes
        `principal` — a resource URI carries no caller identity in this
        protocol, so that path stays the pre-C4 "list everything" behavior)
        and the `list_memory_records` tool, which does. `current_only`
        defaults to **True** here, unlike `GET /memory`, because a resource
        is context an agent reads directly: handing it corrected records
        without the supersedes chain to interpret them would be worse than
        handing it nothing.

        `principal`, when given, filters to records that principal may see
        — security audit finding C4: unfiltered, this returned every other
        principal's `user`/`session`-scope records regardless of
        `security.acl_enforced`. See
        `pheasant.security.acl.is_memory_record_visible`.
        """
        from pheasant.memory.store import MemoryStore, memory_source

        self._require_knowledge_base(knowledge_base)
        source = memory_source(self.config, self.state)
        if source is None:
            return {"enabled": False, "records": []}
        records = MemoryStore(source.path).list_records(scope, current_only=current_only)
        # Phase 3: tier/subsumed_by are SQL-only (earned by a compaction
        # pass, not a file field), so a record's file-based `as_dict()` is
        # annotated with them here rather than the store growing a state
        # handle. Best-effort — a cold/absent projection just means every
        # record reports the pre-Phase-3 default (hot, unsubsumed), same as
        # a region that has never run compaction.
        try:
            compaction = {str(row["record_id"]): row for row in self.state.memory_compaction_rows()}
        except Exception:
            compaction = {}
        identities: set[str] | None = None
        if principal:
            from pheasant.security.acl import expand_principal, is_memory_record_visible
            from pheasant.security.idp import fresh_idp_groups

            identities = expand_principal(principal, None, self.config.security.groups)
            if identities is not None:
                identities |= fresh_idp_groups(self.state, principal, self.config.security.idp)
        out = []
        for record in records:
            if principal and not is_memory_record_visible(
                record.scope, record.written_by, identities
            ):
                continue
            payload = record.as_dict()
            row = compaction.get(record.record_id)
            payload["tier"] = str(row["tier"]) if row and row.get("tier") else "hot"
            payload["subsumed_by"] = row.get("subsumed_by") if row else None
            out.append(payload)
        return {
            "enabled": True,
            "source": source.name,
            "records": out,
        }

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

    def memory_synthesize(self, knowledge_base: str) -> dict:
        """Run one LLM-merge pass now (Phase 4). Off by default and never
        automatic — see `MemorySynthesisSettings`. Merges a near-duplicate
        cluster deterministic compaction could not resolve into one new
        canonical record, subsuming the originals exactly as medoid
        promotion does. Returns `{"skipped": reason}` when synthesis is
        disabled, no `type: memory` source is configured, or no model is
        reachable.
        """
        from pheasant.memory.store import MemoryStore, memory_source
        from pheasant.memory.synthesis import run_synthesis

        self._require_knowledge_base(knowledge_base)
        source = memory_source(self.config, self.state)
        if source is None:
            return {"skipped": "no `type: memory` source is configured"}
        records = MemoryStore(source.path).list_records()
        result = run_synthesis(
            self.engine,
            records,
            self.config.memory.synthesis,
            source,
            allow_private_egress=self.config.security.allow_private_egress,
        )
        self._audit(source.name, "memory_synthesize", "mcp", "mcp", None, utc_now(), result)
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
        section: str | None = None,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
        memory: Any = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Retrieve passages for a query.

        The trailing parameters are **retrieval criteria** an agent can set per
        call rather than having them fixed in config: restrict to one section
        of a document's taxonomy, scope to one source or away from noisy ones,
        keep only certain node types, floor the score, and decide how this
        region's agent memory participates. All are optional and default to the
        pre-existing behavior, so an existing caller is unaffected
        (CLAUDE.md §4 rule 8: additive only).

        ``source_types``/``exclude_source_types`` scope by the *kind* of
        source — ``repository``, ``notion``, ``slack``, ``markdown_folder`` and
        so on — rather than by name. Scoping by name means knowing every source
        in the region first; scoping by type is the question an agent usually
        has ("only what came from our wikis"). Every hit reports its own under
        ``provenance.source_type``, and ``describe_retrieval`` lists the types
        this region actually holds.

        ``section`` matches the heading breadcrumb, so "§ 12.3", "Article IV"
        or a section's wording all reach it and naming a parent returns
        everything nested under it. Only meaningful for sources with taxonomy
        extraction enabled.

        ``memory`` takes one word — ``"off"``, ``"only"``, ``"prefer"`` or the
        default ``"auto"`` — or an object for the rest:
        ``{"scopes": ["user"], "subject": "deploy", "as_of": "2026-01-01T00:00:00Z",
        "current_only": false}``. Corrected records are dropped by default, so
        you do not have to wait for a consolidation pass to stop seeing a fact
        the region already knows was superseded; ``as_of`` deliberately brings
        them back, for asking what was believed at a past instant. Hits that
        came from memory carry a ``memory`` block with the record's scope,
        subject and when it was asserted.
        """
        self._require_knowledge_base(knowledge_base)
        # Over-fetch when a filter will drop rows, so `max_results` still
        # means "give me this many" rather than "look at this many and give me
        # whatever survives" — the latter silently returns short answers.
        filtering = criteria_active(
            exclude_sources, node_types, min_score, source_types, exclude_source_types
        )
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
            section=section,
            memory=memory,
        )
        if filtering:
            payload = dict(payload)
            payload["results"] = apply_retrieval_criteria(
                payload.get("results") or [],
                exclude_sources=exclude_sources,
                node_types=node_types,
                min_score=min_score,
                source_types=source_types,
                exclude_source_types=exclude_source_types,
            )[:max_results]
            payload["criteria"] = criteria_dict(
                source_name,
                exclude_sources,
                node_types,
                min_score,
                memory,
                source_types,
                exclude_source_types,
            )
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
        # What kinds of source this region actually holds, so an agent can pick
        # a `source_types` filter without first listing every source and
        # inspecting each one. Read from the registry, not from config, so a
        # source registered at runtime is included.
        from pheasant.search.criteria import source_type_map

        types = source_type_map(self.state)
        indexed = [str(row["source_id"]) for row in rows]
        source_types = sorted({types[name] for name in indexed if name in types})
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
            "sources": indexed,
            "source_types": source_types,
            "memory": self._describe_memory(),
            "retrieval": settings.model_dump(mode="json"),
            "workflow": self.config.assistant.workflow,
            "workflow_options": dict(self.config.assistant.workflow_options or {}),
            "tunable_per_call": {
                "search_context": [
                    "mode",
                    "max_results",
                    "source_name",
                    "exclude_sources",
                    "source_types",
                    "exclude_source_types",
                    "node_types",
                    "min_score",
                    "memory",
                ],
                "ask_knowledge_base": ["workflow", "mode", "max_results", "options"],
            },
            "field_help": RETRIEVAL_FIELD_HELP,
        }

    def _describe_steering(self) -> dict:
        """The rules that actually re-rank this region's searches (Step 33.8).

        Reported because steering is invisible otherwise: a query that lands
        differently because someone once wrote down a synonym is a mystery
        unless the region can say so.
        """
        if not self.config.memory.steering_enabled:
            return {"enabled": False}
        from pheasant.memory.policy import MemoryPolicy, utc_now_iso
        from pheasant.memory.steering import load_steering, load_steering_records

        rules = load_steering(
            load_steering_records(self.state),
            MemoryPolicy(),
            now=utc_now_iso(),
            enabled=True,
        )
        return {"enabled": True, **rules.describe()}

    def _memory_graph_coverage(self) -> dict:
        """How many records are actually wired into the graph (Step 33.7).

        Computed from the live graph rather than from a stored report, so it
        cannot go stale: "12 records, 9 bridged" is the difference between a
        bridge that is working and one that silently connects nothing, and an
        operator has no other way to tell those apart.
        """
        from pheasant.memory.bridge import ABOUT_EDGE

        try:
            rows = self.state.rows("SELECT artifact_id FROM memory_records")
        except Exception:  # pragma: no cover - state store older than 33.5
            return {}
        artifacts = {str(row["artifact_id"]) for row in rows}
        if not artifacts:
            return {"records": 0, "bridged": 0, "unbridged": 0, "by_signal": {}}

        bridged: set[str] = set()
        by_signal: dict[str, int] = {}
        graph = self.engine.graph_builder.graph
        with graph.reading():
            for (source, _target), edge_map in graph.iter_edges():
                if source not in artifacts:
                    continue
                for data in edge_map.values():
                    if data.get("type") == ABOUT_EDGE:
                        bridged.add(source)
                        signal = str(data.get("match_signal") or "unknown")
                        by_signal[signal] = by_signal.get(signal, 0) + 1
        return {
            "records": len(artifacts),
            "bridged": len(bridged),
            "unbridged": len(artifacts) - len(bridged),
            "by_signal": by_signal,
        }

    def _describe_memory(self) -> dict:
        """What this region remembers, and how a caller may ask for it.

        Without this an agent could only turn memory off by naming the source,
        which it had no way to learn: `sources` is a list of bare ids and
        nothing said which one was the memory. Reporting the scopes and counts
        that actually exist is also the difference between "this region has
        memory" and "this region has memory *about deployments, written this
        week*", which is what decides whether to ask for it at all.
        """
        from pheasant.memory.policy import VALID_MODES
        from pheasant.memory.store import memory_source

        source = memory_source(self.config, self.state)
        if source is None:
            return {"enabled": False}
        try:
            rows = self.state.rows(
                "SELECT scope, COUNT(*) AS n, "
                "SUM(CASE WHEN valid_until IS NULL THEN 1 ELSE 0 END) AS current "
                "FROM memory_records GROUP BY scope ORDER BY scope"
            )
        except Exception:  # pragma: no cover - state store older than 33.5
            rows = []
        return {
            "enabled": True,
            "source_name": source.name,
            "graph": self._memory_graph_coverage(),
            "scopes": {
                str(row["scope"]): {
                    "records": int(row["n"] or 0),
                    "current": int(row["current"] or 0),
                }
                for row in rows
            },
            "modes": list(VALID_MODES),
            "default_mode": self.config.memory.default_policy,
            "steering": self._describe_steering(),
            "accepts": ["mode", "scopes", "subject", "current_only", "as_of", "max_results"],
            "note": (
                "Corrected records are excluded by default; pass "
                "{'current_only': false} or an 'as_of' instant to see them."
            ),
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
        memory: Any = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
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
            memory=memory,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
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
                "memory": memory,
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
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
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
            source_types=source_types,
            exclude_source_types=exclude_source_types,
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
                # One entry per node, not per edge into it — the same fix as
                # `api.app.graph_neighbors`, which this mirrors. `visited`
                # guarded the queue but not the append, so a node reachable by
                # two paths came back twice and `get_graph_slice` built a
                # duplicated `nodes` payload from it.
                if target in visited:
                    continue
                visited.add(target)
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
