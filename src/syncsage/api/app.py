from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from syncsage.config.loader import load_config
from syncsage.config.schema import SyncSageConfig
from syncsage.graph.exporter import cytoscape, node_link
from syncsage.obsidian.exporter import ObsidianExporter
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.registry.knowledge_base_registry import KnowledgeBaseRegistry
from syncsage.registry.source_registry import SourceRegistry
from syncsage.search.hybrid import HybridSearch
from syncsage.search.sqlite_store import SearchStore
from syncsage.sync.engine import SyncEngine


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


def create_app(config: SyncSageConfig | None = None, config_path: str | Path | None = None) -> FastAPI:
    if config is None:
        config = load_config(config_path or "/config/syncsage.yaml")
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    SourceRegistry(config, state).initialize()
    engine = SyncEngine(config, paths, state)
    search = HybridSearch(SearchStore(state))
    app = FastAPI(title="SyncSage", version="0.1.0")
    app.state.config = config
    app.state.state = state
    app.state.engine = engine

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
        return {"sources": SourceRegistry(config, state).list_sources()}

    @app.post("/search")
    def search_context(req: SearchRequest) -> dict:
        return search.search_context(req.knowledge_base or config.knowledge_base_id, req.query, req.mode, req.max_results, req.source_name)

    @app.post("/relevant-files")
    def relevant_files(req: SearchRequest) -> dict:
        payload = search.search_context(req.knowledge_base or config.knowledge_base_id, req.query, "hybrid", req.max_results, req.source_name)
        seen = set()
        files = []
        for result in payload["results"]:
            if result["relative_path"] not in seen:
                seen.add(result["relative_path"])
                files.append(result)
        return {"files": files}

    @app.get("/graph")
    def graph() -> dict:
        return node_link(engine.graph_builder.graph)

    @app.get("/graph/export/node-link-json")
    def graph_node_link() -> dict:
        return node_link(engine.graph_builder.graph)

    @app.get("/graph/export/cytoscape-json")
    def graph_cyto() -> dict:
        return cytoscape(engine.graph_builder.graph)

    @app.post("/obsidian/export")
    def obsidian_export(req: SyncRequest | None = None) -> dict:
        return ObsidianExporter(config, state).export(req.source_name if req else None)

    return app
