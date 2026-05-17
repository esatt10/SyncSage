from __future__ import annotations

import json
from typing import Any


def docker_exec_stdio_config(
    server_name: str = "syncsage",
    container_name: str = "syncsage",
    config_path: str = "/config/syncsage.yaml",
) -> dict[str, Any]:
    """Return a VS Code MCP server config that attaches to a running container."""

    return {
        "servers": {
            server_name: {
                "type": "stdio",
                "command": "docker",
                "args": [
                    "exec",
                    "-i",
                    container_name,
                    "python",
                    "-m",
                    "syncsage",
                    "mcp",
                    "--config",
                    config_path,
                    "--transport",
                    "stdio",
                ],
            }
        }
    }


def docker_run_stdio_config(
    server_name: str = "syncsage",
    image: str = "ghcr.io/esatt10/syncsage:latest",
    config_mount: str = "${workspaceFolder}/syncsage.yaml",
    workspace_mount: str = "${workspaceFolder}",
    vault_mount: str = "${workspaceFolder}/vault",
    state_volume: str = "syncsage-state",
    exports_volume: str = "syncsage-exports",
) -> dict[str, Any]:
    """Return a VS Code MCP server config that starts a foreground Docker container."""

    return {
        "servers": {
            server_name: {
                "type": "stdio",
                "command": "docker",
                "args": [
                    "run",
                    "--rm",
                    "-i",
                    "-e",
                    "SYNCSAGE_CONFIG=/config/syncsage.yaml",
                    "-v",
                    f"{config_mount}:/config/syncsage.yaml:ro",
                    "-v",
                    f"{workspace_mount}:/workspace:ro",
                    "-v",
                    f"{vault_mount}:/vault",
                    "-v",
                    f"{state_volume}:/state",
                    "-v",
                    f"{exports_volume}:/exports",
                    image,
                    "python",
                    "-m",
                    "syncsage",
                    "mcp",
                    "--config",
                    "/config/syncsage.yaml",
                    "--transport",
                    "stdio",
                ],
            }
        }
    }


def render_vscode_mcp_json(config: dict[str, Any]) -> str:
    """Render a stable, human-editable VS Code MCP JSON file."""

    return json.dumps(config, indent=2, sort_keys=False) + "\n"
