from __future__ import annotations

import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from syncsage.config.loader import (
    ConfigError,
    effective_config_dict,
    load_config,
    validate_source_paths,
)
from syncsage.config.profiles import profile_names
from syncsage.config.schema import SourceConfig, SourceType, SyncSageConfig
from syncsage.graph.exporter import cytoscape, node_link
from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.ingestion.pipeline import utc_now
from syncsage.obsidian.exporter import ObsidianExporter
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.registry.knowledge_base_registry import KnowledgeBaseRegistry
from syncsage.registry.source_registry import SourceRegistry
from syncsage.search.hybrid import HybridSearch
from syncsage.search.sqlite_store import SearchStore
from syncsage.security.path_policy import PathPolicyError, resolve_under
from syncsage.sync.engine import SyncEngine
from syncsage.version import __version__

logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    knowledge_base: str | None = None
    query: str
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None


class SyncRequest(BaseModel):
    knowledge_base: str | None = None
    source_name: str | None = None
    mode: str = "incremental"


class ObsidianExportRequest(BaseModel):
    source_name: str | None = None
    preview: bool = False
    template_profile: str | None = None


class RegisterSourceRequest(BaseModel):
    name: str
    type: str = "document_folder"
    path: str
    description: str | None = None
    enabled: bool = True
    include: list[str] | None = None
    exclude: list[str] | None = None
    sync_now: bool = False
    sync_mode: str = "incremental"


class PromoteSourceRequest(BaseModel):
    config_path: str | None = None
    write: bool = False


class ConfigWriteRequest(BaseModel):
    config: dict | None = None
    yaml_text: str | None = None


def _allowed_roots(config: SyncSageConfig) -> list[Path]:
    """Roots a UI may browse / register sources under.

    Mirrors the allowlist used by the MCP register tool plus the exports path.
    """
    roots = [
        config.syncsage.workspace_root,
        config.syncsage.vault_path,
        config.syncsage.exports_path,
    ]
    seen: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


def graph_neighbors(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
) -> dict:
    """Breadth-first neighbor expansion (mirrors SyncSageTools.get_graph_neighbors)."""
    if node_id not in graph:
        return {"node_id": node_id, "depth": depth, "neighbors": []}
    max_depth = max(1, min(int(depth or 1), 10))
    allowed = set(edge_types or [])
    queue: deque = deque([(node_id, 0, [node_id])])
    visited = {node_id}
    neighbors: list[dict] = []
    while queue:
        current, current_depth, path = queue.popleft()
        if current_depth >= max_depth:
            continue
        for _source, target, edge_map in graph.out_edges(current):
            matching = [
                data for data in edge_map.values() if not allowed or data.get("type") in allowed
            ]
            if not matching:
                continue
            next_depth = current_depth + 1
            edge_type_values = sorted({data.get("type") for data in matching if data.get("type")})
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


def graph_slice(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """Connected sub-graph around a node (mirrors SyncSageTools.get_graph_slice)."""
    traversal = graph_neighbors(graph, node_id, depth, edge_types)
    node_ids = [node_id] + [item["node_id"] for item in traversal["neighbors"][:limit]]
    node_set = set(node_ids)
    links = []
    allowed = set(edge_types or [])
    for source in node_set:
        if source not in graph:
            continue
        for _src, target, edge_map in graph.out_edges(source):
            if target not in node_set:
                continue
            for key, data in edge_map.items():
                if allowed and data.get("type") not in allowed:
                    continue
                links.append({"source": source, "target": target, "key": key, **data})
    return {
        "node_id": node_id,
        "depth": depth,
        "nodes": [dict(graph.nodes[item]) for item in node_ids if item in graph],
        "links": links,
    }


def create_app(
    config: SyncSageConfig | None = None,
    config_path: str | Path | None = None,
) -> FastAPI:
    resolved_config_path = (
        str(config_path)
        if config_path
        else os.environ.get("SYNCSAGE_CONFIG", "/config/syncsage.yaml")
    )
    if config is None:
        config = load_config(resolved_config_path)
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    SourceRegistry(config, state).initialize()
    engine = SyncEngine(config, paths, state)
    search = HybridSearch(SearchStore(state))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_sources = [
            source.name for source in config.sources if source.enabled and source.sync.on_startup
        ]
        if startup_sources:
            logger.info("Running startup sync for sources: %s", ", ".join(startup_sources))
            results = engine.startup()
            app.state.startup_sync_results = results
            indexed = sum(result.indexed_artifacts for result in results)
            skipped = sum(result.skipped_artifacts for result in results)
            logger.info(
                "Startup sync complete: sources=%s indexed=%s skipped=%s",
                len(results),
                indexed,
                skipped,
            )
        yield

    app = FastAPI(title="SyncSage", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.state = state
    app.state.engine = engine
    app.state.config_path = resolved_config_path

    # The web UI is a separate workload that talks to this API over HTTP, so the
    # browser origin differs in development. CORS is intentionally permissive for
    # a local-first tool; production deployments should front this with ingress.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def audit(source_id: str | None, action: str, details: dict | None = None) -> None:
        created_at = utc_now()
        ordinal = len(state.list_source_audit_events(source_id, limit=10000))
        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps(
                {"source_id": source_id, "action": action, "created_at": created_at},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        state.append_source_audit_event(
            f"audit:{digest}:{ordinal}",
            source_id,
            action,
            "ui",
            "http",
            None,
            created_at,
            details,
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "syncsage"}

    @app.get("/ready")
    def ready() -> dict:
        return {"status": "ready", "knowledge_base": config.knowledge_base_id}

    @app.get("/metrics")
    def metrics() -> str:
        return "syncsage_up 1\n"

    @app.get("/knowledge-bases")
    def knowledge_bases() -> dict:
        return {"knowledge_bases": KnowledgeBaseRegistry(state).list()}

    @app.get("/sources")
    def sources() -> list[dict]:
        return SourceRegistry(config, state).list_sources()

    @app.post("/sources")
    def register_source(req: RegisterSourceRequest) -> dict:
        try:
            resolved = resolve_under(req.path, _allowed_roots(config))
        except PathPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            source_type = SourceType(req.type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Unknown source type: {req.type}") from exc
        source = SourceConfig(
            name=req.name,
            type=source_type,
            path=resolved,
            description=req.description,
            enabled=req.enabled,
        )
        if req.include is not None:
            source.include = req.include
        if req.exclude is not None:
            source.exclude = req.exclude
        SourceRegistry(config, state).register_source(source)
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        audit(source.name, "register_source", {"source": source.model_dump(mode="json")})
        result = None
        if req.sync_now:
            try:
                result = engine.sync_source(source.name, req.sync_mode).__dict__
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "registered",
            "knowledge_base": config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "sync_result": result,
            "config_update_required": True,
        }

    @app.post("/sources/{source_id}/disable")
    def disable_source(source_id: str) -> dict:
        if not state.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        state.set_source_enabled(source_id, False, "disabled")
        for source in config.sources:
            if source.name == source_id:
                source.enabled = False
        audit(source_id, "disable_source")
        return {"status": "disabled", "source_name": source_id}

    @app.delete("/sources/{source_id}")
    def remove_source(source_id: str) -> dict:
        if not state.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        engine.graph_builder.remove_source_content(source_id)
        engine.graph_store.save(config.knowledge_base_id, engine.graph_builder.graph)
        engine.manifests.delete(source_id)
        state.delete_source(source_id)
        config.sources = [s for s in config.sources if s.name != source_id]
        audit(source_id, "remove_source")
        return {"status": "removed", "source_name": source_id}

    @app.post("/sources/{source_id}/promote")
    def promote_source(source_id: str, req: PromoteSourceRequest | None = None) -> dict:
        req = req or PromoteSourceRequest()
        source = next((s for s in config.sources if s.name == source_id), None)
        if source is None:
            row = state.get_source(source_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            import json

            payload = json.loads(row["config_json"])
            source = SyncSageConfig.model_validate({"sources": [payload]}).sources[0]
        source_payload = source.model_dump(mode="json")
        yaml_patch = yaml.safe_dump({"sources": [source_payload]}, sort_keys=False)
        wrote = False
        target = req.config_path or app.state.config_path
        if req.write:
            path = Path(target)
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            data = data or {}
            existing = [
                item for item in data.get("sources", []) or [] if item.get("name") != source.name
            ]
            data["sources"] = [*existing, source_payload]
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            wrote = True
        audit(source_id, "promote_runtime_source_to_config", {"write": wrote, "path": target})
        return {
            "status": "promoted" if wrote else "patch_generated",
            "source_name": source_id,
            "yaml_patch": yaml_patch,
            "wrote_config": wrote,
            "config_path": target,
        }

    @app.get("/sources/{source_id}/repo-map")
    def repo_map(source_id: str) -> dict:
        rows = state.rows(
            "SELECT relative_path,type,size_bytes FROM artifacts "
            "WHERE source_id=? ORDER BY relative_path",
            (source_id,),
        )
        return {"source_name": source_id, "files": [dict(row) for row in rows]}

    @app.get("/sources/{source_id}/history")
    def source_history(source_id: str, limit: int = 100, offset: int = 0) -> dict:
        return {
            "events": state.list_source_audit_events(source_id, limit, offset),
            "pagination": {"limit": limit, "offset": offset},
        }

    @app.post("/sync")
    def sync_all(req: SyncRequest) -> dict:
        if req.source_name:
            result = engine.sync_source(req.source_name, req.mode)  # type: ignore[arg-type]
            return {"results": [result.__dict__]}
        return {"results": [r.__dict__ for r in engine.sync_all(req.mode)]}  # type: ignore[arg-type]

    @app.post("/sync/{source_id}")
    def sync_source(source_id: str, req: SyncRequest | None = None) -> dict:
        mode = req.mode if req else "incremental"
        try:
            return engine.sync_source(source_id, mode).__dict__  # type: ignore[arg-type]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/sync/status")
    def sync_status() -> dict:
        return {
            "sources": SourceRegistry(config, state).list_sources(),
            "checkpoints": state.list_source_checkpoints(),
        }

    @app.post("/search")
    def search_context(req: SearchRequest) -> dict:
        return search.search_context(
            req.knowledge_base or config.knowledge_base_id,
            req.query,
            req.mode,
            req.max_results,
            req.source_name,
        )

    @app.post("/relevant-files")
    def relevant_files(req: SearchRequest) -> dict:
        payload = search.search_context(
            req.knowledge_base or config.knowledge_base_id,
            req.query,
            "hybrid",
            req.max_results,
            req.source_name,
        )
        seen = set()
        files = []
        for result in payload["results"]:
            if result["relative_path"] not in seen:
                seen.add(result["relative_path"])
                files.append(result)
        return {"files": files}

    @app.get("/files/summary")
    def file_summary(path: str, source_name: str | None = None) -> dict:
        rows = state.rows(
            """SELECT artifacts.*, GROUP_CONCAT(chunks.summary, '\n') AS summary FROM artifacts
            LEFT JOIN chunks ON chunks.artifact_id=artifacts.id
            WHERE artifacts.relative_path=? AND (? IS NULL OR artifacts.source_id=?)
            GROUP BY artifacts.id LIMIT 1""",
            (path, source_name, source_name),
        )
        return dict(rows[0]) if rows else {"path": path, "summary": None}

    @app.get("/graph")
    def graph() -> dict:
        return node_link(engine.graph_builder.graph)

    @app.get("/graph/export/node-link-json")
    def graph_node_link() -> dict:
        return node_link(engine.graph_builder.graph)

    @app.get("/graph/export/cytoscape-json")
    def graph_cyto() -> dict:
        return cytoscape(engine.graph_builder.graph)

    @app.get("/graph/neighbors")
    def graph_neighbors_route(
        node_id: str,
        depth: int = 1,
        edge_types: str | None = None,
    ) -> dict:
        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_neighbors(engine.graph_builder.graph, node_id, depth, types)

    @app.get("/graph/slice")
    def graph_slice_route(
        node_id: str,
        depth: int = 1,
        limit: int = 100,
        edge_types: str | None = None,
    ) -> dict:
        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_slice(engine.graph_builder.graph, node_id, depth, types, limit)

    @app.get("/nodes/explain")
    def explain_node(node_id: str) -> dict:
        g = engine.graph_builder.graph
        if node_id not in g:
            return {"node_id": node_id, "explanation": "Node is not present in the current graph."}
        node = dict(g.nodes[node_id])
        return {
            "node_id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "explanation": f"{node.get('label')} is a {node.get('type')} node indexed by SyncSage.",
            "provenance": node.get("provenance"),
            "node": node,
        }

    @app.get("/fs/list")
    def fs_list(path: str | None = None) -> dict:
        roots = _allowed_roots(config)
        if not path:
            return {
                "path": None,
                "parent": None,
                "roots": [str(root) for root in roots],
                "entries": [
                    {"name": root.name or str(root), "path": str(root), "is_dir": True}
                    for root in roots
                ],
            }
        try:
            resolved = resolve_under(path, roots)
        except PathPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not resolved.exists() or not resolved.is_dir():
            raise HTTPException(status_code=404, detail=f"Not a directory: {resolved}")
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append(
                {"name": child.name, "path": str(child), "is_dir": child.is_dir()}
            )
        parent = resolved.parent
        parent_allowed = any(parent == r or r in parent.parents or parent == r for r in roots)
        return {
            "path": str(resolved),
            "parent": str(parent) if parent_allowed and parent != resolved else None,
            "roots": [str(root) for root in roots],
            "entries": entries,
        }

    @app.get("/config")
    def get_config() -> dict:
        raw_yaml = None
        path = Path(app.state.config_path)
        if path.exists():
            raw_yaml = path.read_text(encoding="utf-8")
        return {
            "path": str(path),
            "effective": config.model_dump(mode="json"),
            "raw_yaml": raw_yaml,
            "profiles": profile_names(),
        }

    @app.put("/config")
    def put_config(req: ConfigWriteRequest) -> dict:
        if req.yaml_text is not None:
            try:
                data = yaml.safe_load(req.yaml_text) or {}
            except yaml.YAMLError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
        elif req.config is not None:
            data = req.config
        else:
            raise HTTPException(status_code=400, detail="Provide config or yaml_text")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Config root must be a mapping")
        try:
            candidate = SyncSageConfig.model_validate(data)
        except (ConfigError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
        errors = validate_source_paths(candidate, require_exists=False)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        path = Path(app.state.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = req.yaml_text if req.yaml_text is not None else yaml.safe_dump(
            data, sort_keys=False
        )
        path.write_text(rendered, encoding="utf-8")
        audit(None, "write_config", {"path": str(path)})
        return {
            "status": "written",
            "path": str(path),
            "restart_required": True,
            "effective": candidate.model_dump(mode="json"),
        }

    @app.get("/config/effective")
    def config_effective(profile: str = "quickstart") -> dict:
        try:
            return effective_config_dict(app.state.config_path, profile, {})
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/obsidian/export")
    def obsidian_export(req: ObsidianExportRequest | None = None) -> dict:
        return ObsidianExporter(config, state).export(
            req.source_name if req else None,
            preview=req.preview if req else False,
            template_profile=req.template_profile if req else None,
        )

    _mount_ui(app, config)
    return app


def _mount_ui(app: FastAPI, config: SyncSageConfig) -> None:
    """Optionally serve a prebuilt UI bundle (Option B in the design doc).

    This is additive and off unless a built bundle exists; the indexing
    container image does not build the UI, so by default nothing is mounted.
    """
    if not config.server.ui.enabled:
        return
    dist = os.environ.get("SYNCSAGE_UI_DIST")
    candidates = [Path(dist)] if dist else []
    candidates.append(Path(__file__).resolve().parents[3] / "ui" / "dist")
    target = next((c for c in candidates if c.exists() and c.is_dir()), None)
    if target is None:
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(target), html=True), name="ui")
    logger.info("Serving SyncSage UI bundle from %s", target)
