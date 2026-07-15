"""MCP client configs for coding agents (Product Framework Step 30.5).

Claude Code (project ``.mcp.json``) and Cursor (``.cursor/mcp.json``) share
one config shape — ``{"mcpServers": {name: {command, args}}}`` — so a single
builder serves both; only the destination file differs. Three attach modes:

- ``local``: the pip-installed ``syncsage`` binary speaks MCP over stdio —
  the zero-docker path that pairs with ``syncsage up``.
- ``docker-exec`` / ``docker-run``: reuse the proven VS Code arg vectors
  (``vscode.py``) for containerized regions.
"""

from __future__ import annotations

import json
from typing import Any

from syncsage.mcp_client.vscode import docker_exec_stdio_config, docker_run_stdio_config

AGENT_CONFIG_FILES = {
    "claude-code": ".mcp.json",
    "cursor": ".cursor/mcp.json",
}


def local_stdio_server(config_path: str = "syncsage.yaml") -> dict[str, Any]:
    """The inner server entry for a locally installed SyncSage."""
    return {
        "command": "syncsage",
        "args": ["mcp", "--config", config_path, "--transport", "stdio"],
    }


def agent_mcp_config(
    mode: str = "local",
    *,
    server_name: str = "syncsage",
    config_path: str = "syncsage.yaml",
    container_name: str = "syncsage",
    image: str | None = None,
) -> dict[str, Any]:
    """Build the ``mcpServers`` document for a coding agent.

    ``mode`` is ``local`` (pip-installed binary), ``docker-exec`` (attach to
    a running container), or ``docker-run`` (start one per session).
    """
    if mode == "local":
        server = local_stdio_server(config_path)
    elif mode == "docker-exec":
        server = docker_exec_stdio_config(server_name, container_name)["servers"][server_name]
    elif mode == "docker-run":
        server = docker_run_stdio_config(server_name, image)["servers"][server_name]
    else:
        raise ValueError(f"unknown mode: {mode!r} (expected local|docker-exec|docker-run)")
    server.pop("type", None)  # Claude Code/Cursor infer stdio from `command`
    return {"mcpServers": {server_name: server}}


def render_agent_mcp_json(config: dict[str, Any]) -> str:
    """Render a stable, human-editable agent MCP JSON file."""
    return json.dumps(config, indent=2, sort_keys=False) + "\n"
