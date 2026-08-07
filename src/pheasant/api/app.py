from __future__ import annotations

import logging
import os
import threading
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pheasant.assistant.credentials import SessionKeyStore
from pheasant.config.loader import (
    ConfigError,
    effective_config_dict,
    load_config,
    validate_source_paths,
)
from pheasant.config.profiles import profile_names
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.graph.exporter import cytoscape, node_link
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.ingestion.pipeline import read_text, utc_now
from pheasant.obsidian.exporter import ObsidianExporter
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.knowledge_base_registry import KnowledgeBaseRegistry
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.security.path_policy import (
    PathPolicyError,
    resolve_config_write_target,
    resolve_under,
)
from pheasant.sync.engine import SyncEngine
from pheasant.sync.fingerprint import EMBEDDING_SCOPE, embedding_fingerprint
from pheasant.version import __version__

logger = logging.getLogger(__name__)

#: Stand-in for the schema's mandatory ``path`` on source types that pull from
#: a service rather than the filesystem. Nothing ever opens it.
PLUGIN_PLACEHOLDER_PATH = "/unused"

#: ``(id, label, description, path_role)`` for the built-in source types, in
#: the order a picker should show them. ``path_role`` is ``"required"`` when
#: the connector actually reads that path and ``"unused"`` when the schema
#: demands the field but the connector gets its content elsewhere.
#: ``SourceType.memory`` is deliberately absent: agent-memory sources are
#: created and owned by the memory store, not registered by hand.
BUILTIN_SOURCE_TYPES: tuple[tuple[str, str, str, str], ...] = (
    (
        "document_folder",
        "Folder of documents",
        "Mixed files — Markdown, PDF, DOCX, HTML, code, configs.",
        "required",
    ),
    (
        "repository",
        "Git repository",
        "A git checkout, with branch and commit metadata on every artifact.",
        "required",
    ),
    (
        "obsidian_vault",
        "Obsidian vault",
        "Notes plus wikilinks, tags and frontmatter as graph structure.",
        "required",
    ),
    (
        "markdown_folder",
        "Folder of Markdown",
        "Markdown only — the lighter version of a document folder.",
        "required",
    ),
    ("single_file", "Single file", "One file, indexed on its own.", "required"),
    (
        "web_collection",
        "Web pages",
        "A list of URLs fetched over HTTP, with ETag-based incremental sync.",
        "unused",
    ),
    ("api", "HTTP API", "A JSON endpoint paged with a cursor (experimental).", "unused"),
    ("s3", "S3 bucket", "An S3-compatible bucket prefix (experimental).", "unused"),
)


class SearchRequest(BaseModel):
    # Step 32.2 — optional caller identity; enforced only when
    # security.acl_enforced is on. The caller (router / deployment
    # perimeter) authenticates; the region enforces visibility.
    principal: str | None = None
    principal_groups: list[str] = []
    knowledge_base: str | None = None
    query: str
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None


class MemoryWriteRequest(BaseModel):
    text: str
    scope: str = "user"
    subject: str | None = None
    supersedes: str | None = None
    tags: list[str] = []
    sync: bool = True


class SyncRequest(BaseModel):
    knowledge_base: str | None = None
    source_name: str | None = None
    mode: str = "incremental"
    # Per-run traversal controls. `depth` caps directory depth for this run;
    # `full_scan` lifts both the depth cap and sync.limits. Neither persists.
    depth: int | None = None
    full_scan: bool = False
    # Default True preserves the documented, tested contract: block until
    # the sync finishes and return its full result. A caller that would
    # rather not hold a connection open for a possibly-long sync (the UI's
    # "sync now" button) sets this false and polls `GET /sources`'
    # `syncing`/`sync_error` fields instead.
    wait: bool = True


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
    max_depth: int | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    repo: dict | None = None
    chunking: dict | None = None
    sync: dict | None = None
    connector: dict | None = None
    # Structural taxonomy extraction (chapters/sections/§ codes). The toggle
    # belongs at registration because it is a property of the *source* — "this
    # corpus is structured documentation" — not of the instance. Omitted or
    # `{"enabled": false}` leaves the source byte-identical to pre-taxonomy.
    taxonomy: dict | None = None
    urls: list[str] | None = None
    sync_now: bool = False
    sync_mode: str = "incremental"
    # See SyncRequest.wait — default True keeps register-then-block the
    # existing behavior; the UI sets this false so registering a source
    # never holds the connection open for however long the first sync
    # takes.
    wait: bool = True


class UpdateSourceRequest(BaseModel):
    type: str | None = None
    path: str | None = None
    description: str | None = None
    enabled: bool | None = None
    max_depth: int | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    taxonomy: dict | None = None
    repo: dict | None = None
    chunking: dict | None = None
    sync: dict | None = None
    connector: dict | None = None
    urls: list[str] | None = None


class PromoteSourceRequest(BaseModel):
    config_path: str | None = None
    write: bool = False


class ConfigWriteRequest(BaseModel):
    config: dict | None = None
    yaml_text: str | None = None


class ChatRequest(BaseModel):
    question: str
    # Opaque handle for a key the user pasted this session. Never the key.
    session_id: str | None = None
    mode: str = "hybrid"
    max_results: int | None = None
    source_name: str | None = None
    principal: str | None = None
    principal_groups: list[str] = []
    # Override assistant.workflow for this one question ("simple",
    # "agentic", or any registered plugin name).
    workflow: str | None = None
    # Per-request workflow knobs, merged over assistant.workflow_options.
    options: dict | None = None


class EmbeddingsRequest(BaseModel):
    """Turn semantic search on/off and configure the provider."""

    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    dimensions: int | None = None
    batch_size: int | None = None
    store_provider: str | None = None
    # Persist to the config file as well as the live process.
    persist: bool = True
    # Build vectors for already-indexed content straight away.
    reindex: bool = False


class AssistantKeyRequest(BaseModel):
    provider: str
    api_key: str
    model: str | None = None
    base_url: str | None = None


class QuickAddRequest(BaseModel):
    """One-field source creation: paste a path or URL and go."""

    target: str
    name: str | None = None
    split: bool = False
    #: Extract a structural taxonomy (chapters/sections/§ codes) from this
    #: source's documents. Off by default, matching the per-source config.
    taxonomy: bool = False
    sync_now: bool = True
    sync_mode: str = "incremental"
    # See SyncRequest.wait — default True keeps the existing, tested
    # register-then-block-on-sync contract; the UI's quick-add sets this
    # false so pasting a large repo's URL never blocks the form (and the
    # reverse proxy in front of it) on however long the first index takes.
    wait: bool = True


def _allowed_roots(config: PheasantConfig) -> list[Path]:
    """Roots a UI may browse / register sources under.

    Mirrors the allowlist used by the MCP register tool plus the exports path.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.vault_path,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen and resolved.exists():
            seen.append(resolved)
    return seen


def _configured_roots(config: PheasantConfig) -> list[Path]:
    """Browse roots for the UI, *including* configured-but-unmounted ones.

    Unlike ``_allowed_roots`` (which drops non-existent roots because they can
    never contain a selectable path), this preserves every configured root so
    the browser can render a root the operator added to
    ``security.allow_workspace_roots`` but forgot to mount — flagged
    ``mounted: false`` — instead of silently hiding it.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.vault_path,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _config_write_roots(config: PheasantConfig) -> list[Path]:
    """Roots a *config file* may be written into.

    Deliberately not ``_allowed_roots``: that one honors
    ``security.allow_user_selected_source_paths``, which widens the list to
    ``/`` so a user can index any folder they like. Choosing what content to
    index is not the same permission as choosing where the server writes
    YAML, and conflating them turns source promotion into an arbitrary file
    write. Only the explicitly configured roots count here.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.vault_path,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _resolve_source_path(path: str, config: PheasantConfig) -> Path:
    if config.security.allow_user_selected_source_paths:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise PathPolicyError(f"Path does not exist: {resolved}")
        return resolved
    return resolve_under(path, _allowed_roots(config))


def _check_source_type(type_name: str) -> bool:
    """Validate a ``sources[].type`` string; return whether it is built-in.

    YAML accepts any type a connector plugin claims (Step 31.1), so the API
    has to as well — otherwise the same source is configurable in a file and
    rejected through the UI.
    """
    from pheasant.sync.connector_registry import list_connector_types

    if type_name in {member.value for member in SourceType}:
        return True
    plugins = list_connector_types()
    if type_name in plugins:
        return False
    raise HTTPException(
        status_code=400,
        detail=f"Unknown source type: {type_name}. Built-in types: "
        + ", ".join(sorted(member.value for member in SourceType))
        + (
            f". Installed connector plugins: {', '.join(plugins)}"
            if plugins
            else ". No connector plugins are installed."
        ),
    )


def _source_from_payload(payload: dict) -> SourceConfig:
    return PheasantConfig.model_validate({"sources": [payload]}).sources[0]


def _source_payload(
    req: RegisterSourceRequest | UpdateSourceRequest,
    resolved_path: Path,
    existing: SourceConfig | None = None,
) -> dict:
    payload = existing.model_dump(mode="json") if existing else {}
    if isinstance(req, RegisterSourceRequest):
        payload.update({"name": req.name, "type": req.type})
        updates = req.model_dump(exclude={"sync_now", "sync_mode"}, exclude_none=True)
    else:
        updates = req.model_dump(exclude_unset=True)
    updates["path"] = str(resolved_path)
    payload.update(updates)
    return payload


#: Edges expanded before anything else at each hop. `contains` is the
#: directory/file hierarchy, and walking it first is what makes a bounded
#: horizon show a *tree* rather than an arbitrary slice of a flat fan-out.
HIERARCHY_EDGE_TYPES = ("contains",)


def graph_neighbors(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    max_nodes: int | None = None,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict:
    """Breadth-first neighbor expansion (mirrors PheasantTools.get_graph_neighbors).

    ``max_nodes`` stops the walk once that many neighbors are collected.
    Callers that only keep the first N (the canvas asks for a bounded horizon)
    must pass it: three hops off a hub node reaches most of the graph, and
    enumerating all of it to then throw nearly all of it away is what made a
    depth-3 slice take minutes. Truncation is in BFS order either way, so the
    kept set is identical — only the work is smaller.
    """
    if node_id not in graph:
        return {"node_id": node_id, "depth": depth, "neighbors": []}
    max_depth = max(1, min(int(depth or 1), 10))
    allowed = set(edge_types or [])
    queue: deque = deque([(node_id, 0, [node_id])])
    visited = {node_id}
    neighbors: list[dict] = []
    while queue:
        if max_nodes is not None and len(neighbors) >= max_nodes:
            break
        current, current_depth, path = queue.popleft()
        if current_depth >= max_depth:
            continue
        # Hierarchy first. A source node carries an `indexes` shortcut to every
        # one of its artifacts, so a plain fan-out spends the whole budget
        # jumping straight to files and the directory tree between them never
        # gets walked — the parent/child structure is present in the graph but
        # invisible in any bounded view of it.
        for _source, target, edge_map in _hierarchy_first(graph.out_edges(current)):
            matching = [
                data
                for data in edge_map.values()
                if (not allowed or data.get("type") in allowed)
                and not (exclude_edge_types and data.get("type") in exclude_edge_types)
            ]
            if not matching:
                continue
            # Types the caller hides are pruned here rather than after the
            # fetch: a concept-heavy graph otherwise spends the entire budget
            # on nodes the view is about to discard, and the structure the
            # caller actually asked for never fits.
            if exclude_node_types:
                target_type = graph.nodes.get(target, {}).get("type")
                if target_type in exclude_node_types:
                    continue
            next_depth = current_depth + 1
            edge_type_values = sorted({data.get("type") for data in matching if data.get("type")})
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
            # A single hub can have thousands of out-edges, so the budget has
            # to bind inside the fan-out, not just between hops.
            if max_nodes is not None and len(neighbors) >= max_nodes:
                break
    return {"node_id": node_id, "depth": depth, "neighbors": neighbors}


def _edge_type_set(value: str | None) -> set[str] | None:
    items = {item.strip() for item in (value or "").split(",") if item.strip()}
    return items or None


def _hierarchy_first(out_edges: list) -> list:
    """Structural edges before shortcuts, order otherwise preserved."""

    def rank(entry) -> int:
        types = {data.get("type") for data in entry[2].values()}
        return 0 if types & set(HIERARCHY_EDGE_TYPES) else 1

    return sorted(out_edges, key=rank)


def graph_slice(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    limit: int = 100,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict:
    """Connected sub-graph around a node (mirrors PheasantTools.get_graph_slice)."""
    traversal = graph_neighbors(
        graph,
        node_id,
        depth,
        edge_types,
        max_nodes=limit,
        exclude_edge_types=exclude_edge_types,
        exclude_node_types=exclude_node_types,
    )
    kept = traversal["neighbors"][:limit]
    node_ids = [node_id] + [item["node_id"] for item in kept]
    node_set = set(node_ids)
    # Hop distance per node, nearest wins (BFS order, so the first sighting is
    # the shortest path). The UI rings the canvas by this and lets the user
    # widen the horizon a layer at a time instead of rendering the whole graph.
    depths: dict[str, int] = {node_id: 0}
    for item in kept:
        target = str(item["node_id"])
        hop = int(item.get("depth") or 0)
        if hop < depths.get(target, hop + 1):
            depths[target] = hop
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
        "nodes": [dict(attrs) for attrs in (graph.nodes.get(item) for item in node_ids) if attrs],
        "links": links,
        "depths": depths,
    }


def create_app(
    config: PheasantConfig | None = None,
    config_path: str | Path | None = None,
) -> FastAPI:
    resolved_config_path = (
        str(config_path)
        if config_path
        else os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml")
    )
    if config is None:
        config = load_config(resolved_config_path)
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    SourceRegistry(config, state).initialize()
    engine = SyncEngine(config, paths, state)
    search = HybridSearch(
        SearchStore(state),
        vector=engine.vector_searcher(),
        node_index=engine.node_index,
        wasm_relationship_search=config.search.wasm_relationship_search,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        startup_sources = [
            source.name for source in config.sources if source.enabled and source.sync.on_startup
        ]
        if startup_sources:
            loop = asyncio.get_running_loop()

            def _run_startup() -> None:
                from pheasant.sync.worker import WorkerBackedEngine

                logger.info("Running startup sync for sources: %s", ", ".join(startup_sources))
                # Indexing happens in a child process so it cannot starve the
                # requests this server exists to answer.
                results = WorkerBackedEngine(engine, app.state.config_path).startup()
                app.state.startup_sync_results = results
                indexed = sum(result.indexed_artifacts for result in results)
                skipped = sum(result.skipped_artifacts for result in results)
                logger.info(
                    "Startup sync complete: sources=%s indexed=%s skipped=%s",
                    len(results),
                    indexed,
                    skipped,
                )

            loop.run_in_executor(None, _run_startup)

        # Warm the agent framework off the request path. Importing langgraph
        # costs ~4s, and it used to be paid, lazily, by whoever asked the first
        # question after a restart — which read as "the planner is slow" when
        # nothing was planning yet. Best-effort and in the background: a
        # missing [agent] extra just means the simple workflow answers.
        def _warm_workflows() -> None:
            from pheasant.assistant.workflows.agentic import warm

            if warm():
                logger.info("Agent workflow ready (langgraph imported and graph compiled)")

        threading.Thread(target=_warm_workflows, name="pheasant-warm", daemon=True).start()
        yield

    app = FastAPI(title="pheasant", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.state = state
    app.state.engine = engine
    app.state.config_path = resolved_config_path
    # Chat API keys a user pastes in the browser live here and nowhere else:
    # process memory, TTL'd, gone on restart.
    app.state.session_keys = SessionKeyStore(config.assistant.session_key_ttl_minutes)
    # Background-sync bookkeeping (`wait=false` on quick-add/register/sync):
    # in-memory only, process lifetime, guarded by one lock. `syncing` holds
    # a source name while its background thread is running; `sync_outcomes`
    # keeps the *last* background result per source (cleared on the next
    # background sync of that source) so a client that missed the transient
    # `syncing` window still sees why it failed instead of just "not syncing".
    app.state.sync_lock = threading.Lock()
    app.state.syncing_sources = {}
    app.state.sync_outcomes = {}

    # The web UI is a separate workload that talks to this API over HTTP, so the
    # browser origin differs in development — but this API is unauthenticated,
    # so a wildcard here means any page in the user's browser can drive every
    # route on it (read the whole index, rewrite the config, register a source
    # over a sensitive directory). Allow the UI's origins, not the web.
    cors_settings = config.server.api
    if cors_settings.cors_allow_all_origins:
        logger.warning(
            "server.api.cors_allow_all_origins is on: every browser origin may call this "
            "unauthenticated API. Only do this behind an authenticating ingress."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if cors_settings.cors_allow_all_origins else cors_settings.cors_origins,
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
        return {"status": "ok", "service": "pheasant"}

    @app.get("/ready")
    def ready() -> dict:
        return {"status": "ready", "knowledge_base": config.knowledge_base_id}

    @app.get("/metrics")
    def metrics() -> str:
        return "pheasant_up 1\n"

    @app.get("/contract")
    def contract() -> dict:
        """Return this region's published semantic contract (Synapse 21.5).

        404 until a contract has been published (a sync with
        ``synapse.publish`` on). Standalone regions that never enable
        publishing simply always 404 here.
        """
        from pheasant.synapse.publisher import load_contract_text

        text = load_contract_text(config.pheasant.state_path)
        if text is None:
            raise HTTPException(status_code=404, detail="No contract published yet")
        import json as _json

        return _json.loads(text)

    @app.get("/knowledge-bases")
    def knowledge_bases() -> dict:
        return {"knowledge_bases": KnowledgeBaseRegistry(state).list()}

    def _with_sync_state(records: list[dict]) -> list[dict]:
        """Overlay live background-sync state (`_run_background_sync`) onto
        persisted source rows, for every route that lists sources — a client
        polling either one sees a source currently syncing, and the outcome
        of the last one that ran in the background even after it finishes:
        `syncing` alone would flicker to nothing the instant a background
        sync completes, which is exactly when a caller most wants to know
        whether it succeeded.
        """
        with app.state.sync_lock:
            syncing = dict(app.state.syncing_sources)
            outcomes = dict(app.state.sync_outcomes)
        for record in records:
            name = record.get("name")
            record["syncing"] = name in syncing
            record["sync_error"] = (outcomes.get(name) or {}).get("error")
        return records

    @app.get("/sources")
    def sources() -> list[dict]:
        return _with_sync_state(SourceRegistry(config, state).list_sources())

    @app.get("/sources/types")
    def source_types() -> dict:
        """Every source type this deployment can register, built-in or plugin.

        The form that creates sources has to offer exactly what YAML accepts,
        which on a machine with connector plugins installed is more than the
        built-in enum. ``path_role`` tells a caller whether the schema's
        mandatory ``path`` is real for that type or just a placeholder the
        connector never reads.
        """
        from pheasant.sync.connector_registry import list_connector_types

        types = [
            {
                "id": type_id,
                "label": label,
                "description": description,
                "path_role": path_role,
                "builtin": True,
            }
            for type_id, label, description, path_role in BUILTIN_SOURCE_TYPES
        ]
        types.extend(
            {
                "id": name,
                "label": name,
                "description": "Connector plugin (pheasant.connectors entry point).",
                # A plugin pulls from its own service; the path field is
                # schema ceremony unless the plugin documents otherwise.
                "path_role": "unused",
                "builtin": False,
            }
            for name in list_connector_types()
        )
        return {"types": types, "placeholder_path": PLUGIN_PLACEHOLDER_PATH}

    @app.post("/sources")
    def register_source(req: RegisterSourceRequest) -> dict:
        is_builtin = _check_source_type(req.type)
        # A plugin connector reads from its own service, not the filesystem,
        # so the schema's mandatory path is a placeholder — don't make the
        # caller invent a real directory to register a Notion or Slack
        # source. A path that *is* supplied still goes through path policy.
        if not is_builtin and req.path.strip() in {"", PLUGIN_PLACEHOLDER_PATH}:
            resolved = Path(PLUGIN_PLACEHOLDER_PATH)
        else:
            try:
                resolved = _resolve_source_path(req.path, config)
            except PathPolicyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            source = _source_from_payload(_source_payload(req, resolved))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
        SourceRegistry(config, state).register_source(source)
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        audit(source.name, "register_source", {"source": source.model_dump(mode="json")})
        result = None
        syncing = False
        if req.sync_now:
            if req.wait:
                try:
                    result = engine.sync_source(source.name, req.sync_mode).__dict__
                except (KeyError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            else:
                _start_background_sync(source.name, req.sync_mode)
                syncing = True
        return {
            "status": "registered",
            "knowledge_base": config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "sync_result": result,
            "syncing": syncing,
            "config_update_required": True,
        }

    @app.put("/sources/{source_id}")
    def update_source(source_id: str, req: UpdateSourceRequest) -> dict:
        source = next((s for s in config.sources if s.name == source_id), None)
        if source is None:
            row = state.get_source(source_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            import json

            source = _source_from_payload(json.loads(row["config_json"]))
        # An edit that doesn't touch the type must not re-validate it: a
        # plugin that got uninstalled shouldn't lock its source out of the
        # form. Only a type the caller actually sends is checked.
        is_builtin = (
            _check_source_type(req.type)
            if req.type is not None
            else str(source.type) in {member.value for member in SourceType}
        )
        if not is_builtin and (req.path or "").strip() in {"", PLUGIN_PLACEHOLDER_PATH}:
            resolved = Path(PLUGIN_PLACEHOLDER_PATH)
        else:
            try:
                resolved = _resolve_source_path(req.path, config) if req.path else source.path
            except PathPolicyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            updated = _source_from_payload(_source_payload(req, resolved, existing=source))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
        engine.graph_builder.remove_source_content(source_id)
        engine.manifests.delete(source_id)
        state.delete_source_artifacts(source_id)
        SourceRegistry(config, state).register_source(updated)
        config.sources = [s for s in config.sources if s.name != source_id]
        config.sources.append(updated)
        engine.graph_store.save(config.knowledge_base_id, engine.graph_builder.graph)
        audit(source_id, "update_source", {"source": updated.model_dump(mode="json")})
        return {
            "status": "updated",
            "source": updated.model_dump(mode="json"),
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
            source = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
        source_payload = source.model_dump(mode="json")
        yaml_patch = yaml.safe_dump({"sources": [source_payload]}, sort_keys=False)
        wrote = False
        # `config_path` is caller-supplied: constrain it to this server's own
        # config (or an allowed root) so promotion cannot be used to write a
        # YAML file anywhere the process can reach.
        try:
            path = resolve_config_write_target(
                req.config_path,
                server_config_path=app.state.config_path,
                allowed_roots=_config_write_roots(config),
            )
        except PathPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = str(path)
        if req.write:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Refusing to overwrite {path}: it is not a pheasant config mapping",
                )
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

    def _index(
        source_name: str | None,
        mode: str,
        depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Index in a worker process, then pick up the result.

        The server deliberately does not index in-process: that work is
        CPU-bound Python and, under the GIL, it starves the very requests this
        API exists to answer. See :mod:`pheasant.sync.worker`.
        """

        from pheasant.sync.worker import WorkerBackedEngine

        worker = WorkerBackedEngine(engine, app.state.config_path)
        if source_name:
            results = [worker.sync_source(source_name, mode, max_depth=depth, full_scan=full_scan)]
        else:
            results = worker.sync_all(mode, max_depth=depth, full_scan=full_scan)
        failed = [r for r in results if r.status in {"failed", "timeout"}]
        if failed:
            raise HTTPException(
                status_code=500,
                detail=failed[0].details.get("error") or f"sync {failed[0].status}",
            )
        return {"results": [r.__dict__ for r in results]}

    def _run_background_sync(source_name: str | None, mode: str, names: list[str]) -> None:
        """The `wait=false` path: same worker-subprocess sync as `_index`,
        off the request thread, outcome recorded instead of returned.

        ``names`` is exactly what `_start_background_sync` already marked
        as syncing (under `sync_lock`, before this thread was even
        started) — every one of them is guaranteed to leave
        `syncing_sources` in the `finally`, success or failure, sync error
        or unexpected exception, so a source can never get stuck showing
        "syncing" forever.
        """
        from pheasant.sync.worker import WorkerBackedEngine

        try:
            worker = WorkerBackedEngine(engine, app.state.config_path)
            if source_name:
                results = [worker.sync_source(source_name, mode)]
            else:
                results = worker.sync_all(mode)
        except Exception as exc:  # background thread — never propagate, just record
            with app.state.sync_lock:
                for name in names:
                    app.state.sync_outcomes[name] = {
                        "status": "error",
                        "error": str(exc),
                        "finished_at": utc_now(),
                    }
            return
        finally:
            with app.state.sync_lock:
                for name in names:
                    app.state.syncing_sources.pop(name, None)
        with app.state.sync_lock:
            for result in results:
                app.state.sync_outcomes[result.source_id] = {
                    "status": result.status,
                    "error": (
                        result.details.get("error")
                        if result.status in {"failed", "timeout"}
                        else None
                    ),
                    "finished_at": utc_now(),
                }

    def _start_background_sync(source_name: str | None, mode: str) -> bool:
        """Returns False (no-op) instead of starting a second overlapping
        pass over a source that is already syncing in the background.

        Found live: a source with `sync.on_startup: true` and no checkpoint
        yet (so every attempt does a full, expensive pass) got resynced by
        both a container-startup trigger and a manual `wait: false` trigger
        landing close together — two concurrent embedding-heavy syncs over
        the same large source, which tripped the OpenAI embeddings
        endpoint's rate limit and multiplied disk/CPU work for no benefit.
        The mark-as-syncing step happens *here*, under one lock acquisition
        that also checks for an existing entry, rather than inside the
        thread — so the check-and-mark is atomic against a second caller
        racing in before the first thread has even started.
        """
        names = [source_name] if source_name else [s.name for s in config.sources if s.enabled]
        with app.state.sync_lock:
            to_start = [n for n in names if n not in app.state.syncing_sources]
            for name in to_start:
                app.state.syncing_sources[name] = {"mode": mode, "started_at": utc_now()}
        if not to_start:
            return False
        threading.Thread(
            target=_run_background_sync,
            args=(source_name, mode, to_start),
            name=f"pheasant-bgsync-{source_name or 'all'}",
            daemon=True,
        ).start()
        return True

    @app.post("/sync")
    def sync_all(req: SyncRequest) -> dict:
        if not req.wait:
            _start_background_sync(None, req.mode)
            return {"status": "syncing", "sources": [s.name for s in config.sources if s.enabled]}
        try:
            return _index(req.source_name, req.mode, req.depth, req.full_scan)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sync/{source_id}")
    def sync_source(source_id: str, req: SyncRequest | None = None) -> dict:
        mode = req.mode if req else "incremental"
        if req is not None and not req.wait:
            # A source registered at runtime (quick-add, POST /sources) lives
            # in the state registry, not necessarily yet in `config.sources`
            # — SyncEngine._source lazily pulls it in from there on first
            # sync. Validating against `config.sources` alone would 404 a
            # perfectly syncable source (e.g. right after a restart, before
            # anything has re-triggered that lazy load).
            known = any(s.name == source_id for s in config.sources) or bool(
                state.get_source(source_id)
            )
            if not known:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            started = _start_background_sync(source_id, mode)
            return {
                "status": "syncing" if started else "already_syncing",
                "source_id": source_id,
            }
        try:
            report = _index(
                source_id,
                mode,
                req.depth if req else None,
                req.full_scan if req else False,
            )
            # This route has always returned one result object, not a list.
            results = report.get("results") or []
            return results[0] if results else report
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sources/{source_id}/scan")
    def scan_source(source_id: str, depth: int | None = None) -> dict:
        """Estimate what a source would index, without indexing it.

        The pre-flight for the "point it at anything" workflow: file count,
        size, where the weight sits, how many files each depth cap admits,
        and whether the configured limits would refuse the sync.
        """
        try:
            return engine.scan_source(source_id, max_depth=depth)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/memory")
    def memory_write(req: MemoryWriteRequest) -> dict:
        from pheasant.memory.store import MemoryStore, memory_source

        source = memory_source(config)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no enabled memory source configured; add a `type: memory` "
                    "source to pheasant.yaml"
                ),
            )
        try:
            record, created = MemoryStore(source.path).append(
                req.text,
                scope=req.scope,
                subject=req.subject,
                supersedes=req.supersedes,
                tags=req.tags,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload: dict = {"record": record.as_dict(), "created": created, "source": source.name}
        if req.sync and created:
            payload["sync"] = engine.sync_source(source.name, "incremental").__dict__
        return payload

    @app.get("/memory")
    def memory_list(scope: str | None = None, current_only: bool = False) -> dict:
        from pheasant.memory.store import MemoryStore, memory_source

        source = memory_source(config)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no enabled memory source configured; add a `type: memory` "
                    "source to pheasant.yaml"
                ),
            )
        try:
            records = MemoryStore(source.path).list_records(scope, current_only=current_only)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"source": source.name, "records": [r.as_dict() for r in records]}

    @app.post("/memory/consolidate")
    def memory_consolidate() -> dict:
        from pheasant.memory.maintenance import run_memory_maintenance

        result = run_memory_maintenance(engine)
        if result is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "memory consolidation is disabled or no `type: memory` source is configured"
                ),
            )
        return result

    @app.post("/security/idp/sync")
    def idp_sync() -> dict:
        from pheasant.security.idp import run_idp_sync

        try:
            return run_idp_sync(config, state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/security/idp/status")
    def idp_sync_status() -> dict:
        from pheasant.security.idp import idp_status

        return idp_status(config, state)

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
            graph=engine.graph_builder.graph,
            principal=req.principal,
            principal_groups=req.principal_groups,
            security=config.security,
        )

    @app.post("/relevant-files")
    def relevant_files(req: SearchRequest) -> dict:
        # Same retrieval as /search, so it must run under the same ACL
        # enforcement: dropping `security`/`principal` here silently returned
        # unfiltered results for every caller whenever acl_enforced was on.
        # No `graph=` on purpose — this route projects to *files*, and graph
        # nodes (concepts, symbols) carry no relative_path, so admitting them
        # would crowd file hits out of the merge and return an empty list.
        payload = search.search_context(
            req.knowledge_base or config.knowledge_base_id,
            req.query,
            "hybrid",
            req.max_results,
            req.source_name,
            principal=req.principal,
            principal_groups=req.principal_groups,
            security=config.security,
        )
        seen = set()
        files = []
        for result in payload["results"]:
            relative_path = result.get("relative_path")
            if relative_path and relative_path not in seen:
                seen.add(relative_path)
                files.append(result)
        return {"files": files}

    def _acl_guard(
        artifact_id: str | None,
        principal: str | None,
        principal_groups: list[str] | None = None,
    ) -> None:
        """403 unless ``principal`` may read ``artifact_id`` (Step 32.2).

        The content endpoints hand back whole artifact bodies by id or path,
        which bypasses the filtering ``search_context`` does — an ACL-enforcing
        region that filters search results but serves the same bytes from
        /files/summary or /nodes/content has not enforced anything. No-op when
        ``security.acl_enforced`` is off, so pre-32 behavior is unchanged.
        """
        if not config.security.acl_enforced:
            return
        from pheasant.security.acl import expand_principal, is_allowed

        identities = expand_principal(principal, principal_groups, config.security.groups)
        if identities is not None and principal:
            from pheasant.security.idp import fresh_idp_groups

            identities |= fresh_idp_groups(state, principal, config.security.idp)
        default_public = config.security.default_visibility != "private"
        acls = state.artifact_acls([artifact_id]) if artifact_id else {}
        if artifact_id not in acls:
            # Not resolvable to an artifact row: deny, matching the
            # conservative rule the search path applies to bare graph nodes.
            raise HTTPException(status_code=403, detail="Not permitted")
        if not is_allowed(acls[artifact_id], identities, default_public=default_public):
            raise HTTPException(status_code=403, detail="Not permitted")

    @app.get("/files/summary")
    def file_summary(
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
    ) -> dict:
        # GROUP_CONCAT order is arbitrary unless the input rows are ordered,
        # so concatenate over ordered scalar subqueries to keep summaries and
        # content in chunk order.
        rows = state.rows(
            """SELECT artifacts.*,
            (SELECT GROUP_CONCAT(summary, '\n') FROM
                (SELECT summary FROM chunks WHERE artifact_id=artifacts.id
                 ORDER BY chunk_index)) AS summary,
            (SELECT GROUP_CONCAT(text, '\n\n') FROM
                (SELECT text FROM chunks WHERE artifact_id=artifacts.id
                 ORDER BY chunk_index)) AS content
            FROM artifacts
            WHERE artifacts.relative_path=? AND (? IS NULL OR artifacts.source_id=?)
            LIMIT 1""",
            (path, source_name, source_name),
        )
        if not rows:
            return {"path": path, "summary": None}
        _acl_guard(str(rows[0]["id"]), principal)
        return dict(rows[0])

    @app.get("/nodes/content")
    def node_content(node_id: str, principal: str | None = None) -> dict:
        graph = engine.graph_builder.graph
        attrs = graph.nodes.get(node_id)
        if attrs is None:
            raise HTTPException(status_code=404, detail=f"Unknown node: {node_id}")
        node = dict(attrs)
        if node.get("type") == "chunk":
            rows = state.rows(
                "SELECT artifact_id, text FROM chunks WHERE id=? LIMIT 1",
                (node_id,),
            )
            if rows:
                _acl_guard(str(rows[0]["artifact_id"]), principal)
            return {"node_id": node_id, "content": rows[0]["text"] if rows else None}
        _acl_guard(node_id, principal)
        artifact_rows = state.rows("SELECT path FROM artifacts WHERE id=? LIMIT 1", (node_id,))
        if artifact_rows:
            path = Path(artifact_rows[0]["path"])
            if path.exists() and path.is_file():
                content = read_text(path)
                if content:
                    return {"node_id": node_id, "content": content}
        # GROUP_CONCAT ignores a trailing ORDER BY, so order the rows in a
        # subquery to keep the reassembled file content in chunk order.
        rows = state.rows(
            "SELECT GROUP_CONCAT(text, '\n\n') AS content FROM "
            "(SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index)",
            (node_id,),
        )
        content = rows[0]["content"] if rows else None
        return {"node_id": node_id, "content": content}

    @app.get("/taxonomy")
    def taxonomy(
        source: str | None = None,
        path: str | None = None,
        max_nodes: int = 2000,
    ) -> dict:
        """The structural outline of taxonomy-enabled documents.

        Reads the `heading` nodes the sync emitted (it does not re-parse), so
        the tree served here is exactly what the graph and the chunks'
        `heading_path` agree on. Nests by the `level` each heading carries,
        which is the same nesting the graph's `contains` edges encode —
        rebuilt here rather than traversed so one document's outline is one
        response, in document order.

        `path` filters to a single document; `source` to one source. A source
        with taxonomy disabled simply has no heading nodes, so it returns an
        empty tree rather than an error.
        """
        from pheasant.ingestion.taxonomy import (
            Ordinal,
            SectionHeading,
            reconcile_issues,
            taxonomy_tree,
        )

        graph_obj = engine.graph_builder.graph
        by_document: dict[str, list[SectionHeading]] = {}
        seen = 0
        for _node_id, data in graph_obj.node_map().items():
            if data.get("type") != "heading":
                continue
            if source and data.get("source_id") != source:
                continue
            relative = str(data.get("relative_path") or "")
            if path and relative != path:
                continue
            seen += 1
            if seen > max(1, max_nodes):
                break
            parts = tuple(int(p) for p in (data.get("ordinal_parts") or []))
            ordinal = (
                Ordinal(
                    parts=parts,
                    series=str(data.get("ordinal_series") or ""),
                    raw=str(data.get("number") or ""),
                    relative=bool(data.get("ordinal_relative")),
                    suffix=str(data.get("ordinal_suffix") or ""),
                )
                if parts
                else None
            )
            by_document.setdefault(relative, []).append(
                SectionHeading(
                    line=int(data.get("start_line") or 0),
                    level=int(data.get("level") or 1),
                    number=data.get("number"),
                    title=str(data.get("title") or ""),
                    kind=str(data.get("kind") or ""),
                    path=str(data.get("heading_path") or ""),
                    pattern_level=int(data.get("pattern_level") or 0),
                    ordinal=ordinal,
                )
            )

        documents = []
        for relative in sorted(by_document):
            headings = sorted(by_document[relative], key=lambda h: h.line)
            issues = reconcile_issues(headings)
            documents.append(
                {
                    "relative_path": relative,
                    "heading_count": len(headings),
                    "tree": taxonomy_tree(headings),
                    # Gaps / duplicates / out-of-order numbering. For contracts
                    # and procedures "is anything missing?" is the question
                    # people actually ask of a document, and once ordinals are
                    # parsed it is nearly free to answer.
                    "issues": issues,
                }
            )
        return {
            "documents": documents,
            "heading_count": sum(d["heading_count"] for d in documents),
            "issue_count": sum(len(d["issues"]) for d in documents),
            "truncated": seen > max(1, max_nodes),
        }

    @app.get("/graph")
    def graph(
        limit: int | None = None,
        link_limit: int | None = None,
        types: str | None = None,
        exclude_types: str | None = None,
        source: str | None = None,
    ) -> dict:
        def _set(value: str | None) -> set[str] | None:
            items = {t.strip() for t in (value or "").split(",") if t.strip()}
            return items or None

        return node_link(
            engine.graph_builder.graph,
            node_limit=limit,
            link_limit=link_limit,
            node_types=_set(types),
            exclude_node_types=_set(exclude_types),
            source_id=source,
        )

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
        exclude_edge_types: str | None = None,
    ) -> dict:
        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_neighbors(
            engine.graph_builder.graph,
            node_id,
            depth,
            types,
            exclude_edge_types=_edge_type_set(exclude_edge_types),
        )

    @app.get("/graph/slice")
    def graph_slice_route(
        node_id: str,
        depth: int = 1,
        limit: int = 100,
        edge_types: str | None = None,
        exclude_edge_types: str | None = None,
        exclude_types: str | None = None,
    ) -> dict:
        """A bounded sub-graph around a node.

        ``exclude_edge_types`` drops edges from the *traversal*, not just the
        output — the canvas uses it to leave out `indexes`, the source→artifact
        shortcut that otherwise flattens the directory tree out of any bounded
        view of the graph.
        """

        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_slice(
            engine.graph_builder.graph,
            node_id,
            depth,
            types,
            limit,
            exclude_edge_types=_edge_type_set(exclude_edge_types),
            exclude_node_types=_edge_type_set(exclude_types),
        )

    @app.get("/nodes/explain")
    def explain_node(node_id: str) -> dict:
        g = engine.graph_builder.graph
        attrs = g.nodes.get(node_id)
        if attrs is None:
            return {"node_id": node_id, "explanation": "Node is not present in the current graph."}
        node = dict(attrs)
        return {
            "node_id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "explanation": f"{node.get('label')} is a {node.get('type')} node indexed by pheasant.",
            "provenance": node.get("provenance"),
            "node": node,
        }

    @app.get("/fs/list")
    def fs_list(path: str | None = None) -> dict:
        roots = _allowed_roots(config)
        if not path:
            browse_roots = _configured_roots(config)
            return {
                "path": None,
                "parent": None,
                "roots": [str(root) for root in browse_roots],
                "entries": [
                    {
                        "name": root.name or str(root),
                        "path": str(root),
                        "is_dir": True,
                        "mounted": root.exists(),
                    }
                    for root in browse_roots
                ],
            }
        try:
            resolved = _resolve_source_path(path, config)
        except PathPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not resolved.exists() or not resolved.is_dir():
            raise HTTPException(status_code=404, detail=f"Not a directory: {resolved}")
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
        parent = resolved.parent
        parent_allowed = config.security.allow_user_selected_source_paths or any(
            parent == r or r in parent.parents or parent == r for r in roots
        )
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
            candidate = PheasantConfig.model_validate(data)
        except (ConfigError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
        errors = validate_source_paths(candidate, require_exists=False)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        path = Path(app.state.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = (
            req.yaml_text if req.yaml_text is not None else yaml.safe_dump(data, sort_keys=False)
        )
        path.write_text(rendered, encoding="utf-8")
        audit(None, "write_config", {"path": str(path)})
        return {
            "status": "written",
            "path": str(path),
            "restart_required": True,
            "effective": candidate.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Semantic search (embeddings). Everything `search.embeddings` does in
    # YAML, reachable from the UI — including building vectors for content
    # that was indexed before embeddings were switched on.
    # ------------------------------------------------------------------
    # Last vector-backend probe failure, so a store that cannot actually run
    # is reported as such instead of surfacing later as a 500 on reindex.
    store_probe_error: dict[str, str | None] = {"error": None}

    def _reload_vector_stack() -> None:
        """Rebuild the embed-on-sync indexer and query-time searcher in place.

        Embeddings are wired at engine construction, so flipping the config
        without this leaves a live process that has agreed to embed and then
        doesn't — the reindex would report success having written nothing.

        Vector backends import lazily, so constructing an indexer proves
        nothing about the backend being usable: a LanceDB store without the
        ``[vector]`` extra builds fine and only fails on first touch. Probe
        it here so enabling embeddings fails at the moment the user asks for
        it, rather than reporting ``active: true`` and 500ing on reindex.
        """
        from pheasant.search.vector_store import vector_indexer_from_config

        indexer = vector_indexer_from_config(config)
        if indexer is not None:
            try:
                indexer.store.count()
            except Exception as exc:
                store_probe_error["error"] = str(exc)
                engine.vectors = None
                search.vector = None
                raise
        store_probe_error["error"] = None
        engine.vectors = indexer
        search.vector = engine.vector_searcher()

    def _embeddings_status() -> dict:
        from pheasant.search.vector_store import (
            VECTOR_STORE_PROVIDERS,
            vector_store_available,
        )

        settings = config.search.embeddings
        vector_count = 0
        dimensions_on_disk: int | None = None
        store_error: str | None = store_probe_error["error"]
        # An indexer exists but that only means it was *constructed*; the
        # backend imports lazily. Touching the store is what proves it works,
        # so a store that raises here is reported inactive rather than
        # advertising a semantic search that would fail on first use.
        active = engine.vectors is not None and store_error is None
        if engine.vectors is not None:
            try:
                vector_count = int(engine.vectors.store.count())
                vectors = getattr(engine.vectors.store, "all_vectors", None)
                if callable(vectors):
                    sample = vectors()
                    if sample:
                        dimensions_on_disk = len(sample[0][1])
            except Exception as exc:  # a broken store must still render a page
                store_error = str(exc)
                active = False
        chunk_rows = state.rows("SELECT COUNT(*) AS n FROM chunks")
        chunk_count = int(chunk_rows[0]["n"]) if chunk_rows else 0
        return {
            "enabled": settings.enabled,
            "active": active,
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "api_key_env": settings.api_key_env,
            "api_key_present": bool(os.environ.get(settings.api_key_env)),
            "dimensions": settings.dimensions,
            "batch_size": settings.batch_size,
            "store_provider": config.search.vector_store.provider,
            "store_path": str(config.search.vector_store.path or ""),
            "vector_count": vector_count,
            "chunk_count": chunk_count,
            # How much of the index is actually searchable semantically.
            "coverage": round(vector_count / chunk_count, 4) if chunk_count else 0.0,
            "dimensions_on_disk": dimensions_on_disk,
            "store_error": store_error,
            "providers": [
                {
                    "id": "stub",
                    "label": "Deterministic stub (offline)",
                    "needs_key": False,
                    "description": "No network, no model. Useful for trying semantic "
                    "search out and for tests.",
                },
                {
                    "id": "openai-spec",
                    "label": "OpenAI-spec endpoint",
                    "needs_key": True,
                    "description": "POST {base_url}/embeddings — OpenAI, or any "
                    "gateway or self-hosted server speaking the same shape.",
                },
            ],
            "store_providers": [
                {
                    "id": name,
                    "label": label,
                    "available": vector_store_available(name),
                    "hint": hint,
                }
                for name, label, hint in VECTOR_STORE_PROVIDERS
            ],
        }

    @app.get("/search/embeddings")
    def embeddings_status() -> dict:
        return _embeddings_status()

    @app.put("/search/embeddings")
    def embeddings_update(req: EmbeddingsRequest) -> dict:
        from pheasant.search.vector_store import (
            VECTOR_STORE_PROVIDERS,
            vector_store_available,
        )

        settings = config.search.embeddings
        # Refuse a backend this deployment cannot run *before* touching the
        # config, so a bad choice leaves the running process exactly as it was
        # instead of half-applied and inert.
        target_store = req.store_provider or config.search.vector_store.provider
        wants_on = settings.enabled if req.enabled is None else req.enabled
        if wants_on and not vector_store_available(target_store):
            hint = next(
                (hint for name, _, hint in VECTOR_STORE_PROVIDERS if name == target_store),
                None,
            )
            raise HTTPException(
                status_code=400,
                detail=f"Vector store {target_store!r} is not available in this deployment"
                + (f" — {hint}" if hint else "")
                + ". The 'numpy' store needs no extra dependencies.",
            )
        changes = {
            key: value
            for key, value in {
                "enabled": req.enabled,
                "provider": req.provider,
                "model": req.model,
                "base_url": req.base_url,
                "api_key_env": req.api_key_env,
                "dimensions": req.dimensions,
                "batch_size": req.batch_size,
            }.items()
            if value is not None
        }
        # Changing the model or dimension invalidates every existing vector:
        # a store mixing two embedding spaces returns nonsense similarities.
        # Detect it here so the caller is told rather than finding out later.
        dimension_change = (
            "dimensions" in changes and changes["dimensions"] != settings.dimensions
        ) or ("model" in changes and changes["model"] != settings.model)

        for key, value in changes.items():
            setattr(settings, key, value)
        if req.store_provider is not None:
            config.search.vector_store.provider = req.store_provider

        try:
            _reload_vector_stack()
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not enable embeddings: {exc}"
            ) from exc

        wrote_config = False
        if req.persist:
            payload = {
                "search": {
                    "embeddings": settings.model_dump(mode="json"),
                    "vector_store": config.search.vector_store.model_dump(mode="json"),
                }
            }
            wrote_config = _merge_into_config_file(payload)
        audit(None, "update_embeddings", {"changes": list(changes), "persisted": wrote_config})

        result = {
            "status": "updated",
            "wrote_config": wrote_config,
            "vectors_invalidated": dimension_change,
            **_embeddings_status(),
        }
        if req.reindex and engine.vectors is not None:
            result["reindex"] = _rebuild_vectors(drop_existing=dimension_change)
            # Record the space we just embedded into, so the next restart sees
            # a matching fingerprint and does not drop and re-embed all over
            # again for a change that has already been applied.
            state.set_fingerprint(EMBEDDING_SCOPE, embedding_fingerprint(config), utc_now())
        return result

    def _rebuild_vectors(drop_existing: bool = False) -> dict:
        """Embed everything already indexed, without re-reading the sources.

        Vectors are keyed by content-addressed chunk id, so this is
        idempotent: chunks already embedded are skipped and a second run
        makes zero embedder calls. The engine owns the same operation (it
        runs it automatically when the embedding config changes under a
        restart); this wrapper adds the HTTP-facing error shape.
        """
        if engine.vectors is None:
            raise HTTPException(
                status_code=400,
                detail=store_probe_error["error"]
                or "Embeddings are not enabled — turn them on first.",
            )
        embedded = 0
        artifacts = state.rows("SELECT id, source_id FROM artifacts ORDER BY id")
        try:
            if drop_existing:
                # A changed model/dimension means the old vectors are garbage.
                for source_id in {
                    str(row["source_id"])
                    for row in state.rows("SELECT DISTINCT source_id FROM artifacts")
                }:
                    engine.vectors.prune_source(source_id, set())

            for artifact in artifacts:
                chunks = state.rows(
                    "SELECT id, text, text_hash FROM chunks WHERE artifact_id=? "
                    "ORDER BY chunk_index",
                    (artifact["id"],),
                )
                if not chunks:
                    continue
                embedded += engine.vectors.index_artifact(
                    str(artifact["source_id"]),
                    str(artifact["id"]),
                    [dict(chunk) for chunk in chunks],
                )
            # index_artifact queues rather than embeds immediately (batches
            # across artifacts so this loop isn't one embedder call per
            # artifact) — flush before reading the count, or a remainder
            # smaller than the queue threshold would be silently unembedded
            # and undercounted.
            engine.vectors.flush()
            vector_count = int(engine.vectors.store.count())
        except (ModuleNotFoundError, ValueError) as exc:
            # A missing optional extra or a mismatched embedding space is a
            # configuration problem the caller can act on, not a server fault.
            store_probe_error["error"] = str(exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(None, "rebuild_vectors", {"embedded_chunks": embedded})
        return {
            "embedded_chunks": embedded,
            "artifacts_scanned": len(artifacts),
            "vector_count": vector_count,
        }

    @app.post("/search/embeddings/reindex")
    def embeddings_reindex(drop_existing: bool = False) -> dict:
        return {**_rebuild_vectors(drop_existing=drop_existing), **_embeddings_status()}

    def _merge_into_config_file(patch: dict) -> bool:
        """Deep-merge ``patch`` into the config file, preserving everything else.

        Rendered the same way ``pheasant up`` renders configs: everything but
        ``sources`` through the YAML dumper, then the sources list by hand —
        ``safe_dump`` writes list-of-dict items in a shape the
        dependency-light yaml shim cannot read back.
        """
        from pheasant.config.loader import deep_merge
        from pheasant.quickstart import _render_sources_block

        path = Path(app.state.config_path)
        if not path.exists():
            return False
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return False
        if not isinstance(existing, dict):
            return False
        merged = deep_merge(existing, patch)
        sources = merged.pop("sources", []) or []
        rendered = yaml.safe_dump(merged, sort_keys=False)
        if sources:
            rendered += _render_sources_block(sources)
        path.write_text(rendered, encoding="utf-8")
        return True

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

    # ------------------------------------------------------------------
    # Overview — one call that tells the UI whether there is anything to
    # show yet, and what shape it is. Drives the onboarding empty state.
    # ------------------------------------------------------------------
    @app.get("/overview")
    def overview() -> dict:
        graph_obj = engine.graph_builder.graph
        # One pinned read of aggregates the graph maintains on write. This
        # used to walk every node to tally types on an endpoint the UI hits on
        # every page load — seconds of latency for numbers already known.
        with graph_obj.reading():
            counts = graph_obj.type_counts()
            total_nodes = graph_obj.number_of_nodes()
            total_links = graph_obj.number_of_edges()
        registered = _with_sync_state(SourceRegistry(config, state).list_sources())
        artifacts = state.rows("SELECT COUNT(*) AS n FROM artifacts")
        chunks = state.rows("SELECT COUNT(*) AS n FROM chunks")
        indexed = int(artifacts[0]["n"]) if artifacts else 0
        # "Content" excludes the knowledge-base root and its source nodes:
        # a freshly-created KB has those and nothing else, and showing a
        # lone star node as if it were a graph is exactly the confusion
        # this field exists to prevent.
        structural = counts.get("knowledge_base", 0) + counts.get("source", 0)
        return {
            "knowledge_base": config.knowledge_base_id,
            "name": config.pheasant.name,
            "description": config.pheasant.description,
            "version": __version__,
            "sources": registered,
            "source_count": len(registered),
            "indexed_artifacts": indexed,
            "chunk_count": int(chunks[0]["n"]) if chunks else 0,
            "node_counts": counts,
            "total_nodes": total_nodes,
            "total_links": total_links,
            "has_content": total_nodes > structural and indexed > 0,
            "config_path": str(app.state.config_path),
        }

    # ------------------------------------------------------------------
    # Assistant — grounded chat over the index. Never in the sync path.
    # ------------------------------------------------------------------
    @app.get("/assistant/status")
    def assistant_status(session_id: str | None = None) -> dict:
        from pheasant.assistant.providers import PROVIDERS, resolve_auto_provider

        settings = config.assistant
        providers = [
            {
                "id": spec.id,
                "label": spec.label,
                "default_model": spec.default_model,
                "api_key_env": spec.api_key_env,
                "key_hint": spec.key_hint,
                # Whether the *server* already holds a key for this provider.
                "env_key_present": bool(os.environ.get(spec.api_key_env)),
            }
            for spec in PROVIDERS.values()
        ]
        from pheasant.assistant.chat import resolve_provider

        credential = app.state.session_keys.get(session_id)
        configured = settings.provider
        if configured == "auto":
            configured = resolve_auto_provider(dict(os.environ))
        # `ready` must mean "a credential actually resolves", not "a provider
        # name is configured" — otherwise a config naming a provider whose key
        # env var is unset reports "model connected" while every answer comes
        # back extractive. Resolve exactly the way /assistant/chat does.
        selected = resolve_provider(config, credential, dict(os.environ))
        return {
            "enabled": settings.enabled,
            "providers": providers,
            "configured_provider": configured,
            "configured_model": settings.model,
            "allow_session_keys": settings.allow_session_keys,
            "session": credential.redacted() if credential else None,
            # False means answers will be extractive rather than synthesized.
            "ready": selected is not None,
            "credential_source": selected.get("source") if selected else None,
        }

    @app.post("/assistant/key")
    def assistant_key(req: AssistantKeyRequest) -> dict:
        """Hold a user-supplied key in memory for this session only.

        The key never leaves this process: not to disk, not to the config,
        not into any response body. The caller gets an opaque session id.
        """
        from pheasant.assistant.providers import PROVIDERS

        if not config.assistant.allow_session_keys:
            raise HTTPException(
                status_code=403,
                detail="Session-supplied API keys are disabled (assistant.allow_session_keys)",
            )
        if req.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")
        if not req.api_key.strip():
            raise HTTPException(status_code=400, detail="API key must not be empty")
        token, credential = app.state.session_keys.put(
            req.provider,
            req.api_key.strip(),
            model=req.model,
            base_url=req.base_url,
        )
        return {"session_id": token, "session": credential.redacted()}

    @app.delete("/assistant/key")
    def assistant_key_revoke(session_id: str | None = None) -> dict:
        return {"revoked": app.state.session_keys.revoke(session_id)}

    @app.post("/assistant/chat")
    def assistant_chat(req: ChatRequest) -> dict:
        from pheasant.assistant.chat import answer_question

        if not config.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is disabled")
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")
        return answer_question(
            req.question,
            search=search,
            knowledge_base=config.knowledge_base_id,
            config=config,
            graph=engine.graph_builder.graph,
            state=state,
            credential=app.state.session_keys.get(req.session_id),
            env=dict(os.environ),
            mode=req.mode,
            max_results=req.max_results,
            source_name=req.source_name,
            principal=req.principal,
            principal_groups=req.principal_groups,
            workflow=req.workflow,
            options=req.options,
        )

    @app.post("/assistant/chat/stream")
    def assistant_chat_stream(req: ChatRequest):
        """The same answer as ``/assistant/chat``, with progress as it happens.

        Server-sent events: one ``step`` per workflow stage the moment it
        finishes, then a single ``answer`` (or ``error``) and the stream
        closes. The agent loop can take a while over a large index, and a
        client that shows "planning… retrieved 35 passages… grading" is
        waiting rather than wondering. The work runs in a worker thread and
        the steps arrive through a queue, so a slow reader can never stall the
        workflow itself.
        """

        import json as json_module
        import queue as queue_module
        import threading

        from starlette.responses import StreamingResponse

        from pheasant.assistant.chat import answer_question

        if not config.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is disabled")
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")

        events: queue_module.Queue = queue_module.Queue()
        credential = app.state.session_keys.get(req.session_id)
        environ = dict(os.environ)

        def run() -> None:
            try:
                answer = answer_question(
                    req.question,
                    search=search,
                    knowledge_base=config.knowledge_base_id,
                    config=config,
                    graph=engine.graph_builder.graph,
                    state=state,
                    credential=credential,
                    env=environ,
                    mode=req.mode,
                    max_results=req.max_results,
                    source_name=req.source_name,
                    principal=req.principal,
                    principal_groups=req.principal_groups,
                    workflow=req.workflow,
                    options=req.options,
                    on_step=lambda step: events.put(
                        {
                            "type": "step",
                            "name": step.name,
                            "detail": step.detail,
                            "passages": step.passages,
                        }
                    ),
                )
                events.put({"type": "answer", "answer": answer})
            except Exception as exc:  # surfaced to the client, never a 500 mid-stream
                logger.exception("streaming chat failed")
                events.put({"type": "error", "error": str(exc)})
            finally:
                events.put(None)

        threading.Thread(target=run, name="pheasant-chat-stream", daemon=True).start()

        def publish():
            while True:
                item = events.get()
                if item is None:
                    return
                yield f"data: {json_module.dumps(item)}\n\n"

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx sits in front of this in the compose stack and would
                # otherwise buffer the whole stream into one write.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/assistant/workflows")
    def assistant_workflows() -> dict:
        """Every question-answering workflow this deployment can run."""
        from pheasant.assistant.chat import resolve_provider
        from pheasant.assistant.workflows import (
            langgraph_available,
            list_workflows,
            resolve_workflow_name,
        )
        from pheasant.assistant.workflows.agentic import DEFAULTS as AGENTIC_DEFAULTS

        selected = resolve_provider(config, None, dict(os.environ))
        return {
            "workflows": list_workflows(),
            "configured": config.assistant.workflow,
            "active": resolve_workflow_name(
                config.assistant.workflow, has_llm=selected is not None
            ),
            "agent_extra_installed": langgraph_available(),
            "options": dict(config.assistant.workflow_options or {}),
            "option_defaults": {"agentic": AGENTIC_DEFAULTS},
        }

    # ------------------------------------------------------------------
    # MCP connection details, so the UI can hand an agent a working config.
    # ------------------------------------------------------------------
    @app.get("/mcp/info")
    def mcp_info() -> dict:
        from pheasant.mcp_client.agents import agent_mcp_config, render_agent_mcp_json

        transports = dict(config.server.mcp.transports)
        base = f"http://{config.server.host}:{config.server.port}"
        clients = {}
        for client_name in ("claude-code", "cursor"):
            try:
                clients[client_name] = render_agent_mcp_json(
                    agent_mcp_config("local", config_path=str(app.state.config_path))
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("could not render %s mcp config: %s", client_name, exc)
        return {
            "enabled": config.server.mcp.enabled,
            "transports": transports,
            "streamable_http_url": f"{base}/mcp" if transports.get("streamable_http") else None,
            "stdio_command": ["pheasant", "mcp", "--transport", "stdio"],
            "config_path": str(app.state.config_path),
            "tools": _mcp_tool_summaries(),
            "client_configs": clients,
        }

    # ------------------------------------------------------------------
    # Quick add — the UI counterpart to `pheasant up <anything>`.
    # ------------------------------------------------------------------
    @app.post("/sources/quick-add")
    def quick_add(req: QuickAddRequest) -> dict:
        from pheasant.targets import TargetError, fetch_target, resolve_targets

        state_dir = Path(config.pheasant.state_path)
        try:
            targets = resolve_targets(
                [req.target],
                clone_root=state_dir / "sources",
                workspace=state_dir / "external",
                split=req.split,
                name=req.name,
            )
            for target in targets:
                fetch_target(target)
        except TargetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        registry = SourceRegistry(config, state)
        created: list[dict] = []
        for target in targets:
            payload = target.to_source_dict()
            if req.taxonomy:
                payload["taxonomy"] = {"enabled": True}
            if target.local:
                try:
                    payload["path"] = str(_resolve_source_path(payload["path"], config))
                except PathPolicyError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                source = _source_from_payload(payload)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
            registry.register_source(source)
            # The engine resolves sources off the live config object, so a
            # freshly registered source is invisible to sync until it lands
            # there too (same step the /sources route takes).
            config.sources = [s for s in config.sources if s.name != source.name]
            config.sources.append(source)
            audit(source.name, "quick_add", {"target": req.target, "type": target.type})
            created.append(payload)

        results = []
        syncing: list[str] = []
        if req.sync_now:
            if req.wait:
                from dataclasses import asdict

                for entry in created:
                    try:
                        results.append(asdict(engine.sync_source(entry["name"], req.sync_mode)))
                    except Exception as exc:  # surfaced per-source; others still run
                        results.append(
                            {"source_id": entry["name"], "status": "error", "error": str(exc)}
                        )
            else:
                # Cloning (above) still happens on this request — it's the
                # bounded part. Indexing is the unbounded part (a big repo's
                # first full parse+chunk+embed pass can run well past any
                # reverse-proxy timeout), so that's what moves to the
                # background; the caller gets sources back already
                # registered and polls GET /sources' `syncing` field.
                for entry in created:
                    _start_background_sync(entry["name"], req.sync_mode)
                    syncing.append(entry["name"])
        return {
            "status": "registered",
            "sources": created,
            "sync_results": results,
            "syncing": syncing,
        }

    _mount_ui(app, config)
    return app


def _mcp_tool_summaries() -> list[dict]:
    """Name + one-line description for each public MCP tool.

    Read off ``PheasantTools`` rather than hand-maintained, so a tool added
    there shows up in the UI without a second edit (rule 8: the tool
    surface is public API, and this is one of its shop windows).
    """
    from pheasant.mcp_server.tools import PheasantTools

    summaries = []
    for name in sorted(dir(PheasantTools)):
        if name.startswith("_"):
            continue
        member = getattr(PheasantTools, name, None)
        if not callable(member):
            continue
        doc = (member.__doc__ or "").strip().splitlines()
        summaries.append({"name": name, "description": doc[0] if doc else ""})
    return summaries


def _mount_ui(app: FastAPI, config: PheasantConfig) -> None:
    """Optionally serve a prebuilt UI bundle (Option B in the design doc).

    This is additive and off unless a built bundle exists; the indexing
    container image does not build the UI, so by default nothing is mounted.
    """
    # Recorded either way so callers (the CLI's startup banner) can tell the
    # user whether the UI is actually being served, instead of leaving them to
    # guess why the port shows JSON.
    app.state.ui_dist = None
    if not config.server.ui.enabled:
        return
    dist = os.environ.get("PHEASANT_UI_DIST")
    candidates = [Path(dist)] if dist else []
    candidates.append(Path(__file__).resolve().parents[3] / "ui" / "dist")
    target = next((c for c in candidates if c.exists() and c.is_dir()), None)
    if target is None:
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(target), html=True), name="ui")
    app.state.ui_dist = str(target)
    logger.info("Serving pheasant UI bundle from %s", target)
