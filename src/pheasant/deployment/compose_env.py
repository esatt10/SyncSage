from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from pheasant.mcp_client.vscode import DEFAULT_IMAGE_REPOSITORY
from pheasant.version import __version__

ENV_ORDER = (
    "PHEASANT_IMAGE",
    "PHEASANT_CONFIG_PATH",
    "PHEASANT_WORKSPACE_PATH",
    "PHEASANT_VAULT_PATH",
    "PHEASANT_DATA_PATH",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _compose_host_path(value: str) -> str:
    if (
        not value
        or value.startswith((".", "/", "\\", "~", "$"))
        or (len(value) >= 3 and value[1] == ":" and value[2] in ("\\", "/"))
    ):
        return value
    return f"./{value}"


def load_compose_environment(config_path: str | Path) -> dict[str, str]:
    """Load Docker Compose interpolation variables from a pheasant YAML file."""

    selected_config = Path(config_path)
    data = yaml.safe_load(selected_config.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError("Config root must be a mapping")

    deployment = _mapping(data.get("deployment"))
    compose = _mapping(deployment.get("compose"))

    image = compose.get("image")
    if image is None:
        repository = _string(compose.get("image_repository"), DEFAULT_IMAGE_REPOSITORY)
        tag = _string(compose.get("image_tag"), __version__)
        image = f"{repository}:{tag}" if tag else repository

    return {
        "PHEASANT_IMAGE": str(image),
        "PHEASANT_CONFIG_PATH": _compose_host_path(
            _string(compose.get("config_path"), str(config_path))
        ),
        "PHEASANT_WORKSPACE_PATH": _compose_host_path(
            _string(compose.get("workspace_path"), "./workspace")
        ),
        "PHEASANT_VAULT_PATH": _compose_host_path(_string(compose.get("vault_path"), "./vault")),
        # Extra read-only bind mount for local files that live OUTSIDE the
        # workspace, surfaced in the container at /data (an allowed source root).
        "PHEASANT_DATA_PATH": _compose_host_path(_string(compose.get("data_path"), "./data")),
    }


def _quote_env_value(value: str) -> str:
    if value and not any(char.isspace() or char in "#'\"" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_env_file(values: Mapping[str, str]) -> str:
    """Render values in Docker Compose env-file format."""

    lines = []
    for key in ENV_ORDER:
        if key in values:
            lines.append(f"{key}={_quote_env_value(values[key])}")
    for key in sorted(set(values) - set(ENV_ORDER)):
        lines.append(f"{key}={_quote_env_value(values[key])}")
    return "\n".join(lines) + "\n"
