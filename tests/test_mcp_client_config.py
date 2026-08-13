"""Tests for generated MCP client configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pheasant.mcp_client.agents import (
    AGENT_CONFIG_FILES,
    agent_mcp_config,
    render_agent_mcp_json,
)
from pheasant.mcp_client.vscode import (
    docker_exec_stdio_config,
    docker_run_stdio_config,
    render_vscode_mcp_json,
)


def test_vscode_docker_exec_config_uses_running_pheasant_container() -> None:
    config = docker_exec_stdio_config()
    server = config["servers"]["pheasant"]

    assert server["type"] == "stdio"
    assert server["command"] == "docker"
    assert server["args"][:3] == ["exec", "-i", "pheasant"]
    assert "mcp" in server["args"]
    assert "/config/pheasant.yaml" in server["args"]


def test_vscode_docker_run_config_contains_no_user_home_paths() -> None:
    rendered = render_vscode_mcp_json(docker_run_stdio_config())
    parsed = json.loads(rendered)
    args = parsed["servers"]["pheasant"]["args"]

    assert "${workspaceFolder}/pheasant.yaml:/config/pheasant.yaml:ro" in args
    assert "${workspaceFolder}:/workspace:ro" in args
    assert "~" not in rendered


def test_agent_config_local_mode_uses_installed_binary() -> None:
    parsed = json.loads(render_agent_mcp_json(agent_mcp_config("local")))
    server = parsed["mcpServers"]["pheasant"]
    assert server["command"] == "pheasant"
    assert server["args"] == ["mcp", "--config", "pheasant.yaml", "--transport", "stdio"]
    assert "type" not in server


def test_agent_config_docker_modes_reuse_vscode_arg_vectors() -> None:
    exec_server = agent_mcp_config("docker-exec")["mcpServers"]["pheasant"]
    assert exec_server["command"] == "docker"
    assert exec_server["args"][:3] == ["exec", "-i", "pheasant"]

    run_server = agent_mcp_config("docker-run")["mcpServers"]["pheasant"]
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
    from pheasant.cli import main

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
    assert parsed["mcpServers"]["pheasant"]["args"][1:3] == ["--config", "my.yaml"]


def test_mcp_and_serve_honour_the_pheasant_config_env_var(tmp_path, monkeypatch) -> None:
    """`PHEASANT_CONFIG` must reach the servers that pheasant tells agents to set it for.

    The Dockerfile sets it, docker-entrypoint.sh reads it, troubleshooting.md
    says to check it — and `deployment/host.py` / `mcp_client/vscode.py` write
    it into the generated MCP client config handed to Claude Code, Cursor and
    VS Code. But `pheasant mcp` and `pheasant serve` hardcoded
    `/config/pheasant.yaml` as their `--config` default, so an agent pointed at
    any other config silently searched the wrong knowledge base and reported
    the results as if they were the right ones. Caught live: a config written
    to a second path was ignored, and the server answered from the default.
    """
    from pathlib import Path

    from pheasant import cli

    relocated = tmp_path / "elsewhere.yaml"
    relocated.write_text(
        f"""pheasant:
  name: relocated-kb
  state_path: {tmp_path / "state"}
  vault_path: {tmp_path / "vault"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHEASANT_CONFIG", str(relocated))

    seen: dict[str, str] = {}

    def fake_run(config, transport="stdio"):
        seen["name"] = config.pheasant.name
        seen["transport"] = transport

    monkeypatch.setattr("pheasant.mcp_server.server.run_mcp_server", fake_run)
    assert cli.main(["mcp"]) == 0
    assert seen["name"] == "relocated-kb", (
        "pheasant mcp ignored PHEASANT_CONFIG and loaded the hardcoded default"
    )

    # An explicit --config still wins over the environment.
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(
        relocated.read_text(encoding="utf-8").replace("relocated-kb", "explicit-kb"),
        encoding="utf-8",
    )
    seen.clear()
    assert cli.main(["mcp", "--config", str(explicit)]) == 0
    assert seen["name"] == "explicit-kb"

    # `serve` resolves from the same place.
    served: dict[str, str] = {}
    monkeypatch.setattr(
        cli, "_serve_app", lambda cfg, path, **kw: served.update(name=cfg.pheasant.name)
    )
    assert cli.main(["serve"]) == 0
    assert served["name"] == "relocated-kb"
    assert Path(str(relocated)).exists()
