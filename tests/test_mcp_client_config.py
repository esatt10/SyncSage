"""Tests for generated MCP client configuration."""

from __future__ import annotations

import json

from syncsage.mcp_client.vscode import (
    docker_exec_stdio_config,
    docker_run_stdio_config,
    render_vscode_mcp_json,
)


def test_vscode_docker_exec_config_uses_running_syncsage_container() -> None:
    config = docker_exec_stdio_config()
    server = config["servers"]["syncsage"]

    assert server["type"] == "stdio"
    assert server["command"] == "docker"
    assert server["args"][:3] == ["exec", "-i", "syncsage"]
    assert "mcp" in server["args"]
    assert "/config/syncsage.yaml" in server["args"]


def test_vscode_docker_run_config_contains_no_user_home_paths() -> None:
    rendered = render_vscode_mcp_json(docker_run_stdio_config())
    parsed = json.loads(rendered)
    args = parsed["servers"]["syncsage"]["args"]

    assert "${workspaceFolder}/syncsage.yaml:/config/syncsage.yaml:ro" in args
    assert "${workspaceFolder}:/workspace:ro" in args
    assert "~" not in rendered
