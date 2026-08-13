from __future__ import annotations

import json
import logging
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.graph.exporter import node_link
from pheasant.mcp_server.tools import PheasantTools

logger = logging.getLogger(__name__)


def create_mcp_tools(config: PheasantConfig) -> PheasantTools:
    """Return a tool facade usable by MCP adapters or direct tests.

    The optional official MCP SDK can wrap this facade at runtime; keeping the core
    implementation dependency-light makes CLI/API tests deterministic.
    """

    return PheasantTools(config)


def create_mcp_server(config: PheasantConfig) -> Any:
    """Create the official MCP SDK server around the pheasant tool facade."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The MCP runtime is not installed. Install pheasant with the mcp extra "
            "or use the Docker image, which includes it."
        ) from exc

    tools = create_mcp_tools(config)
    mcp = FastMCP(
        "pheasant",
        instructions=(
            "Use pheasant to sync configured knowledge sources, search indexed context, "
            "inspect graph relationships, and export Obsidian-compatible notes."
        ),
        stateless_http=True,
        json_response=True,
    )
    _configure_transport_settings(mcp, config)

    @mcp.tool()
    def list_knowledge_bases() -> dict:
        """Return registered knowledge bases and their status."""

        return tools.list_knowledge_bases()

    @mcp.tool()
    def register_source(
        knowledge_base: str,
        name: str,
        source_type: str,
        path: str,
        description: str | None = None,
        enabled: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        taxonomy: bool = False,
    ) -> dict:
        """Register a source path after allowlisted path validation.

        Set ``taxonomy`` for structured documentation (books, procedures,
        legal documents): each artifact's outline — Chapter / Article /
        Section / § / 1.2.3 / (a) — is then extracted on every sync, chunks
        are cut and labelled per section, and `heading` graph nodes are
        emitted. Off by default.
        """

        return tools.register_source(
            knowledge_base=knowledge_base,
            name=name,
            source_type=source_type,
            path=path,
            description=description,
            enabled=enabled,
            include=include,
            exclude=exclude,
            taxonomy=taxonomy,
        )

    @mcp.tool()
    def list_sources(
        knowledge_base: str,
        enabled: bool | None = None,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List sources with optional filters and pagination."""

        return tools.list_sources(knowledge_base, enabled, status, source_type, limit, offset)

    @mcp.tool()
    def disable_source(knowledge_base: str, source_name: str) -> dict:
        """Disable a source without deleting its state."""

        return tools.disable_source(knowledge_base, source_name)

    @mcp.tool()
    def remove_source(knowledge_base: str, source_name: str) -> dict:
        """Remove a source and its indexed state."""

        return tools.remove_source(knowledge_base, source_name)

    @mcp.tool()
    def promote_runtime_source_to_config(
        knowledge_base: str,
        source_name: str,
        config_path: str | None = None,
        write: bool = False,
    ) -> dict:
        """Return a deterministic YAML patch or write a source into durable config."""

        return tools.promote_runtime_source_to_config(
            knowledge_base,
            source_name,
            config_path,
            write,
        )

    @mcp.tool()
    def sync_source(
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Sync one configured source.

        A source that would exceed its configured size limits returns
        ``status: "limit_exceeded"`` and indexes nothing — call
        ``scan_source`` first to see the shape, then either narrow it with
        ``max_depth`` or re-run with ``full_scan=True``.
        """

        return tools.sync_source(
            knowledge_base, source_name, mode, max_depth=max_depth, full_scan=full_scan
        )

    @mcp.tool()
    def scan_source(
        knowledge_base: str,
        source_name: str,
        max_depth: int | None = None,
    ) -> dict:
        """Estimate what a source would index, without indexing anything."""

        return tools.scan_source(knowledge_base, source_name, max_depth=max_depth)

    @mcp.tool()
    def memory_write(
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
        """Persist one agent-memory record (session/user/org scope) and index it.

        The memory becomes retrievable via search_context immediately when
        sync=true (read-your-writes). Requires a `type: memory` source.

        principal records who asserted the memory; pass it whenever you know
        the caller's identity, since it is what keeps one agent's memories
        distinct from another's. kind marks a record as retrieval policy
        (alias/preference/exclusion) instead of a fact. valid_until gives the
        record an expiry it declares itself.
        """

        return tools.memory_write(
            knowledge_base,
            text,
            scope,
            subject,
            supersedes,
            tags,
            sync,
            kind,
            principal,
            valid_until,
        )

    @mcp.tool()
    def memory_consolidate(knowledge_base: str) -> dict:
        """Archive superseded/expired memory records and re-index the memory source."""

        return tools.memory_consolidate(knowledge_base)

    @mcp.tool()
    def sync_all(
        knowledge_base: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Sync all enabled configured sources."""

        return tools.sync_all(knowledge_base, mode, max_depth=max_depth, full_scan=full_scan)

    @mcp.tool()
    def search_context(  # noqa: PLR0913 - additive principal params (32.2)
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
        memory: dict | str | None = None,
    ) -> dict:
        """Search indexed context and return compact results with provenance.

        principal/principal_groups scope results to what that caller may see
        when security.acl_enforced is on (Step 32.2); ignored otherwise.

        section restricts results to one part of a document's extracted
        taxonomy, matched against the breadcrumb — "§ 12.3", "Article IV" or a
        section's wording all work, and naming a parent returns everything
        nested under it. Only meaningful for sources with taxonomy enabled.

        source_name/exclude_sources/node_types/min_score are retrieval
        criteria you can set per call instead of relying on how the region
        was configured. Call describe_retrieval to see what this region
        offers, and preview_retrieval to compare criteria against the
        standing configuration before committing to them.

        memory controls how this region's agent memory takes part: one of
        "auto" (default), "off", "only", "prefer", or an object such as
        {"scopes": ["user"], "subject": "deploy", "as_of": "2026-01-01T00:00:00Z"}.
        Records a later record corrected are excluded by default; pass an
        as_of instant to ask what was believed at that time. Results that came
        from memory carry a "memory" block naming the record and its scope.
        """

        return tools.search_context(
            knowledge_base,
            query,
            mode,
            max_results,
            include_chunks,
            include_graph_neighbors,
            principal=principal,
            principal_groups=principal_groups,
            section=section,
            source_name=source_name,
            exclude_sources=exclude_sources,
            node_types=node_types,
            min_score=min_score,
            memory=memory,
        )

    @mcp.tool()
    def describe_retrieval(knowledge_base: str) -> dict:
        """Report how this knowledge base retrieves, and what you can override.

        Returns the standing configuration (default mode, result count,
        retrieval rounds and depth), which search modes actually work here
        (semantic search is only offered when a vector index exists), the
        sources present, and the criteria each retrieval tool accepts per
        call. Call this before guessing at parameters for an unfamiliar
        region.
        """

        return tools.describe_retrieval(knowledge_base)

    @mcp.tool()
    def preview_retrieval(  # noqa: PLR0913 - mirrors search_context's criteria
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
        memory: dict | str | None = None,
    ) -> dict:
        """Try retrieval criteria and see how they differ from the configuration.

        Runs the given criteria and the region's configured retrieval over the
        same query, then reports both result sets and the delta (added,
        dropped, kept). Use it to test a setting against real content before
        anyone writes it into pheasant.yaml. Read-only — nothing is persisted.
        """

        return tools.preview_retrieval(
            knowledge_base,
            query,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            exclude_sources=exclude_sources,
            node_types=node_types,
            min_score=min_score,
            memory=memory,
        )

    @mcp.tool()
    def ask_knowledge_base(  # noqa: PLR0913 - mirrors the HTTP surface
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

        Runs the configured agent workflow over pheasant's own search. Use
        this for a synthesized, cited answer; use search_context when you
        want the raw passages to reason over yourself.
        """

        return tools.ask_knowledge_base(
            knowledge_base,
            question,
            workflow=workflow,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
            options=options,
        )

    @mcp.tool()
    def get_relevant_files(
        knowledge_base: str,
        task: str,
        source_name: str | None = None,
        max_files: int = 8,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Return files likely needed for a coding or research task."""

        return tools.get_relevant_files(
            knowledge_base,
            task,
            source_name,
            max_files,
            principal=principal,
            principal_groups=principal_groups,
        )

    @mcp.tool()
    def get_graph_neighbors(
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> dict:
        """Return graph neighbors around a node."""

        return tools.get_graph_neighbors(knowledge_base, node_id, depth, edge_types)

    @mcp.tool()
    def get_file_summary(
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
    ) -> dict:
        """Return summary and provenance for one indexed file."""

        return tools.get_file_summary(knowledge_base, path, source_name)

    @mcp.tool()
    def get_repo_map(knowledge_base: str, source_name: str, depth: int = 3) -> dict:
        """Return a compact repository map for one source."""

        return tools.get_repo_map(knowledge_base, source_name, depth)

    @mcp.tool()
    def explain_node(knowledge_base: str, node_id: str) -> dict:
        """Explain what an indexed graph node represents."""

        return tools.explain_node(knowledge_base, node_id)

    @mcp.tool()
    def export_obsidian_notes(
        knowledge_base: str,
        source_name: str | None = None,
        scope: str = "knowledge_base",
        preview: bool = False,
        template_profile: str | None = None,
    ) -> dict:
        """Write or update Obsidian-compatible Markdown notes."""

        return tools.export_obsidian_notes(
            knowledge_base,
            source_name,
            scope,
            preview,
            template_profile,
        )

    @mcp.tool()
    def get_sync_status(knowledge_base: str) -> dict:
        """Return source freshness and last sync status."""

        return tools.get_sync_status(knowledge_base)

    @mcp.tool()
    def get_contract(knowledge_base: str) -> dict:
        """Return this region's published Synapse semantic contract."""

        return tools.get_contract(knowledge_base)

    @mcp.tool()
    def get_sync_history(
        knowledge_base: str,
        source_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Return runtime source lifecycle history."""

        return tools.get_sync_history(knowledge_base, source_name, limit, offset)

    @mcp.resource("pheasant://knowledge-bases")
    def knowledge_bases_resource() -> str:
        """Return registered knowledge bases as JSON."""

        return _json(tools.list_knowledge_bases())

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources")
    def sources_resource(kb_id: str) -> str:
        """Return configured source status for a knowledge base as JSON."""

        return _json(tools.get_sync_status(kb_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/graph")
    def graph_resource(kb_id: str) -> str:
        """Return the current graph snapshot as JSON."""

        tools._require_knowledge_base(kb_id)
        return _json(node_link(tools.engine.graph_builder.graph))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources/{source_id}/manifest")
    def source_manifest_resource(kb_id: str, source_id: str) -> str:
        """Return the manifest for one source as JSON."""

        return _json(tools.get_source_manifest(kb_id, source_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources/{source_id}/history")
    def source_history_resource(kb_id: str, source_id: str) -> str:
        """Return lifecycle history for one source as JSON."""

        return _json(tools.get_sync_history(kb_id, source_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sync-history")
    def sync_history_resource(kb_id: str) -> str:
        """Return lifecycle history for a knowledge base as JSON."""

        return _json(tools.get_sync_history(kb_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources/{source_id}/repo-map")
    def source_repo_map_resource(kb_id: str, source_id: str) -> str:
        """Return a repository map resource for one source as JSON."""

        return _json(tools.get_repo_map(kb_id, source_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/nodes/{node_id}")
    def node_resource(kb_id: str, node_id: str) -> str:
        """Return an explanation for one graph node as JSON."""

        return _json(tools.explain_node(kb_id, node_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/graph-slices/{node_id}")
    def graph_slice_resource(kb_id: str, node_id: str) -> str:
        """Return a small graph slice rooted at one node as JSON."""

        return _json(tools.get_graph_slice(kb_id, node_id, depth=2))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/memory")
    def memory_resource(kb_id: str) -> str:
        """Return this region's current agent-memory records as JSON.

        A *resource* rather than only a tool because "what does this region
        remember" is context to read, not an action to take — an agent can
        pull it into a conversation the way it pulls a file, without spending
        a tool call on a search that may or may not surface the record it
        needs. Corrected records are excluded; use `search_context` with an
        `as_of` instant to see what was believed at a past time.
        """

        return _json(tools.memory_list(kb_id, current_only=True))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/contract")
    def contract_resource(kb_id: str) -> str:
        """Return this region's published Synapse semantic contract as JSON."""

        return _json(tools.get_contract(kb_id))

    @mcp.prompt()
    def use_pheasant_for_coding_task(task: str = "") -> str:
        """Guide an agent through a pheasant-backed coding workflow."""

        suffix = f"\nTask: {task}" if task else ""
        return (
            "Call get_relevant_files for the task, inspect returned provenance, make the "
            "smallest safe change, run checks, then call sync_source with mode=incremental."
            f"{suffix}"
        )

    @mcp.prompt()
    def use_pheasant_for_document_research(query: str = "") -> str:
        """Guide an agent through provenance-first document research."""

        suffix = f"\nQuery: {query}" if query else ""
        return (
            "Use search_context first. Prefer chunks with explicit provenance, avoid claims "
            "beyond retrieved evidence, and call get_graph_neighbors for related material."
            f"{suffix}"
        )

    return mcp


def _apply_transport_security(settings: Any, config: PheasantConfig) -> None:
    """Point FastMCP's DNS-rebinding guard at pheasant's own CORS policy.

    FastMCP defaults the guard to ``127.0.0.1``/``localhost``/``[::1]`` only,
    which is right for a laptop and wrong for the container this normally runs
    in: reach the very same server as ``http://pheasant:8765`` or by LAN IP and
    every MCP request answers **421 Misdirected Request**.

    Rather than invent a second allow-list, this derives one from
    ``server.api.cors_origins`` — the knob an operator already uses to say who
    may reach this API — and honours ``cors_allow_all_origins`` as the same
    documented escape hatch it already is. FastMCP's localhost defaults are
    kept alongside, so this only ever *widens* to hosts the operator already
    admitted, and a config that never mentions CORS behaves as FastMCP intends.
    """
    from urllib.parse import urlsplit

    security = getattr(settings, "transport_security", None)
    if security is None:
        return
    api = config.server.api
    if getattr(api, "cors_allow_all_origins", False):
        # The operator has declared this API open (their own authenticating
        # ingress fronts it). Refusing MCP on host grounds here would be
        # inconsistent with every other route on the same port.
        security.enable_dns_rebinding_protection = False
        return
    hosts = list(security.allowed_hosts)
    origins = list(security.allowed_origins)
    for origin in api.cors_origins or []:
        parsed = urlsplit(origin)
        if not parsed.netloc:
            continue
        if parsed.netloc not in hosts:
            hosts.append(parsed.netloc)
        if origin not in origins:
            origins.append(origin)
    security.allowed_hosts = hosts
    security.allowed_origins = origins


def streamable_http_app(config: PheasantConfig) -> Any:
    """The MCP streamable-HTTP ASGI app, for mounting inside the API server.

    ``pheasant serve`` advertises ``streamable_http_url: <base>/mcp`` from
    ``GET /mcp/info`` whenever ``server.mcp.transports.streamable_http`` is on
    (the default) — but it only ever built the FastAPI app, so that URL 404'd
    and a client POSTing to it got a 405. Found by pointing a real MCP client
    at a live container: the server was telling every agent to connect
    somewhere it did not listen.

    Returns None when the transport is disabled or the installed ``mcp``
    package cannot build the app, so the caller mounts nothing and the API
    behaves exactly as it did.
    """
    if not config.server.mcp.enabled:
        return None
    if not config.server.mcp.transports.get("streamable_http", False):
        return None
    try:
        mcp = create_mcp_server(config)
        # The route inside FastMCP's app becomes the *mount* point's suffix, so
        # its internal path has to be "/" — leaving it at "/mcp" while mounting
        # at "/mcp" would serve "/mcp/mcp". Mounting the app at "/" instead
        # would put an ASGI catch-all ahead of the UI's static files.
        settings = getattr(mcp, "settings", None)
        if settings is not None:
            settings.streamable_http_path = "/"
            _apply_transport_security(settings, config)
        return mcp.streamable_http_app()
    except Exception:  # pragma: no cover - depends on the installed mcp version
        logger.warning("MCP streamable-http app unavailable; /mcp not mounted", exc_info=True)
        return None


def run_mcp_server(config: PheasantConfig, transport: str = "stdio") -> None:
    """Run the MCP server with the selected transport."""

    mcp = create_mcp_server(config)
    if transport == "stdio":
        mcp.run(transport=transport)
        return
    path = "/mcp" if transport == "streamable-http" else "/sse"
    try:
        mcp.run(transport=transport, host=config.server.host, port=config.server.port, path=path)
    except TypeError:
        mcp.run(transport=transport)


def _configure_transport_settings(mcp: Any, config: PheasantConfig) -> None:
    settings = getattr(mcp, "settings", None)
    if settings is None:
        return
    for name, value in {
        "host": config.server.host,
        "port": config.server.port,
        "streamable_http_path": "/mcp",
        "sse_path": "/sse",
    }.items():
        if hasattr(settings, name):
            setattr(settings, name, value)


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2, sort_keys=True)
