"""Tests for generated MCP client configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncsage.mcp_client.agents import (
    AGENT_CONFIG_FILES,
    agent_mcp_config,
    render_agent_mcp_json,
)
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


def test_agent_config_local_mode_uses_installed_binary() -> None:
    parsed = json.loads(render_agent_mcp_json(agent_mcp_config("local")))
    server = parsed["mcpServers"]["syncsage"]
    assert server["command"] == "syncsage"
    assert server["args"] == ["mcp", "--config", "syncsage.yaml", "--transport", "stdio"]
    assert "type" not in server


def test_agent_config_docker_modes_reuse_vscode_arg_vectors() -> None:
    exec_server = agent_mcp_config("docker-exec")["mcpServers"]["syncsage"]
    assert exec_server["command"] == "docker"
    assert exec_server["args"][:3] == ["exec", "-i", "syncsage"]

    run_server = agent_mcp_config("docker-run")["mcpServers"]["syncsage"]
    assert run_server["command"] == "docker"
    assert "${workspaceFolder}:/workspace:ro" in run_server["args"]


def test_agent_config_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        agent_mcp_config("teleport")


def test_agent_config_files_cover_both_agents() -> None:
    assert AGENT_CONFIG_FILES == {
        "claude-code": ".mcp.json",
        "cursor": ".cursor/mcp.json",
    }


def test_cli_client_config_claude_code_writes_mcp_json(tmp_path: Path) -> None:
    from syncsage.cli import main

    out = tmp_path / ".mcp.json"
    rc = main(
        [
            "client-config",
            "claude-code",
            "--config",
            "my.yaml",
            "--output",
            str(out),
        ]
    )
    assert rc == 0
    parsed = json.loads(out.read_text())
    assert parsed["mcpServers"]["syncsage"]["args"][1:3] == ["--config", "my.yaml"]
