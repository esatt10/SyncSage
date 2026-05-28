from __future__ import annotations

import json
from typing import Any

from syncsage.config.schema import SyncSageConfig
from syncsage.graph.exporter import node_link
from syncsage.mcp_server.tools import SyncSageTools


def create_mcp_tools(config: SyncSageConfig) -> SyncSageTools:
    """Return a tool facade usable by MCP adapters or direct tests.

    The optional official MCP SDK can wrap this facade at runtime; keeping the core
    implementation dependency-light makes CLI/API tests deterministic.
    """

    return SyncSageTools(config)


def create_mcp_server(config: SyncSageConfig) -> Any:
    """Create the official MCP SDK server around the SyncSage tool facade."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The MCP runtime is not installed. Install SyncSage with the mcp extra "
            "or use the Docker image, which includes it."
        ) from exc

    tools = create_mcp_tools(config)
    mcp = FastMCP(
        "SyncSage",
        instructions=(
            "Use SyncSage to sync configured knowledge sources, search indexed context, "
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
    ) -> dict:
        """Register a source path after allowlisted path validation."""

        return tools.register_source(
            knowledge_base=knowledge_base,
            name=name,
            source_type=source_type,
            path=path,
            description=description,
            enabled=enabled,
            include=include,
            exclude=exclude,
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
    def sync_source(knowledge_base: str, source_name: str, mode: str = "incremental") -> dict:
        """Sync one configured source."""

        return tools.sync_source(knowledge_base, source_name, mode)

    @mcp.tool()
    def sync_all(knowledge_base: str, mode: str = "incremental") -> dict:
        """Sync all enabled configured sources."""

        return tools.sync_all(knowledge_base, mode)

    @mcp.tool()
    def search_context(
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        include_chunks: bool = True,
        include_graph_neighbors: bool = True,
    ) -> dict:
        """Search indexed context and return compact results with provenance."""

        return tools.search_context(
            knowledge_base,
            query,
            mode,
            max_results,
            include_chunks,
            include_graph_neighbors,
        )

    @mcp.tool()
    def get_relevant_files(
        knowledge_base: str,
        task: str,
        source_name: str | None = None,
        max_files: int = 8,
    ) -> dict:
        """Return files likely needed for a coding or research task."""

        return tools.get_relevant_files(knowledge_base, task, source_name, max_files)

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
    def get_sync_history(
        knowledge_base: str,
        source_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Return runtime source lifecycle history."""

        return tools.get_sync_history(knowledge_base, source_name, limit, offset)

    @mcp.resource("syncsage://knowledge-bases")
    def knowledge_bases_resource() -> str:
        """Return registered knowledge bases as JSON."""

        return _json(tools.list_knowledge_bases())

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/sources")
    def sources_resource(kb_id: str) -> str:
        """Return configured source status for a knowledge base as JSON."""

        return _json(tools.get_sync_status(kb_id))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/graph")
    def graph_resource(kb_id: str) -> str:
        """Return the current graph snapshot as JSON."""

        tools._require_knowledge_base(kb_id)
        return _json(node_link(tools.engine.graph_builder.graph))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/sources/{source_id}/manifest")
    def source_manifest_resource(kb_id: str, source_id: str) -> str:
        """Return the manifest for one source as JSON."""

        return _json(tools.get_source_manifest(kb_id, source_id))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/sources/{source_id}/history")
    def source_history_resource(kb_id: str, source_id: str) -> str:
        """Return lifecycle history for one source as JSON."""

        return _json(tools.get_sync_history(kb_id, source_id))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/sync-history")
    def sync_history_resource(kb_id: str) -> str:
        """Return lifecycle history for a knowledge base as JSON."""

        return _json(tools.get_sync_history(kb_id))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/sources/{source_id}/repo-map")
    def source_repo_map_resource(kb_id: str, source_id: str) -> str:
        """Return a repository map resource for one source as JSON."""

        return _json(tools.get_repo_map(kb_id, source_id))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/nodes/{node_id}")
    def node_resource(kb_id: str, node_id: str) -> str:
        """Return an explanation for one graph node as JSON."""

        return _json(tools.explain_node(kb_id, node_id))

    @mcp.resource("syncsage://knowledge-bases/{kb_id}/graph-slices/{node_id}")
    def graph_slice_resource(kb_id: str, node_id: str) -> str:
        """Return a small graph slice rooted at one node as JSON."""

        return _json(tools.get_graph_slice(kb_id, node_id, depth=2))

    @mcp.prompt()
    def use_syncsage_for_coding_task(task: str = "") -> str:
        """Guide an agent through a SyncSage-backed coding workflow."""

        suffix = f"\nTask: {task}" if task else ""
        return (
            "Call get_relevant_files for the task, inspect returned provenance, make the "
            "smallest safe change, run checks, then call sync_source with mode=incremental."
            f"{suffix}"
        )

    @mcp.prompt()
    def use_syncsage_for_document_research(query: str = "") -> str:
        """Guide an agent through provenance-first document research."""

        suffix = f"\nQuery: {query}" if query else ""
        return (
            "Use search_context first. Prefer chunks with explicit provenance, avoid claims "
            "beyond retrieved evidence, and call get_graph_neighbors for related material."
            f"{suffix}"
        )

    return mcp


def run_mcp_server(config: SyncSageConfig, transport: str = "stdio") -> None:
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


def _configure_transport_settings(mcp: Any, config: SyncSageConfig) -> None:
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
