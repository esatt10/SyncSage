from __future__ import annotations

import json
from typing import Any

from pheasant.version import __version__

DEFAULT_IMAGE_REPOSITORY = "ghcr.io/esatt10/pheasant"
DEFAULT_IMAGE = f"{DEFAULT_IMAGE_REPOSITORY}:{__version__}"


def docker_exec_stdio_config(
    server_name: str = "pheasant",
    container_name: str = "pheasant",
    config_path: str = "/config/pheasant.yaml",
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
                    "pheasant",
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
    server_name: str = "pheasant",
    image: str | None = None,
    config_mount: str = "${workspaceFolder}/pheasant.yaml",
    workspace_mount: str = "${workspaceFolder}",
    state_volume: str = "pheasant-state",
    exports_volume: str = "pheasant-exports",
) -> dict[str, Any]:
    """Return a VS Code MCP server config that starts a foreground Docker container."""

    image = image or DEFAULT_IMAGE

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
                    "PHEASANT_CONFIG=/config/pheasant.yaml",
                    "-v",
                    f"{config_mount}:/config/pheasant.yaml:ro",
                    "-v",
                    f"{workspace_mount}:/workspace:ro",
                    "-v",
                    f"{state_volume}:/state",
                    "-v",
                    f"{exports_volume}:/exports",
                    image,
                    "python",
                    "-m",
                    "pheasant",
                    "mcp",
                    "--config",
                    "/config/pheasant.yaml",
                    "--transport",
                    "stdio",
                ],
            }
        }
    }


def render_vscode_mcp_json(config: dict[str, Any]) -> str:
    """Render a stable, human-editable VS Code MCP JSON file."""

    return json.dumps(config, indent=2, sort_keys=False) + "\n"
