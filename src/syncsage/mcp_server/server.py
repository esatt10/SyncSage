from __future__ import annotations

from syncsage.config.schema import SyncSageConfig
from syncsage.mcp_server.tools import SyncSageTools


def create_mcp_tools(config: SyncSageConfig) -> SyncSageTools:
    """Return a tool facade usable by MCP adapters or direct tests.

    The optional official MCP SDK can wrap this facade at runtime; keeping the core
    implementation dependency-light makes CLI/API tests deterministic.
    """

    return SyncSageTools(config)
