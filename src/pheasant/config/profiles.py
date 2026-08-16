from __future__ import annotations

from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "quickstart": {
        "pheasant": {"environment": "local", "log_level": "INFO"},
        "server": {
            "host": "127.0.0.1",
            "port": 8765,
            "mcp": {"enabled": True, "transports": {"stdio": True, "streamable_http": True}},
        },
        "sync": {"watcher": {"enabled": True}, "scheduler": {"enabled": True}},
    },
    "dev": {
        "pheasant": {"environment": "dev", "log_level": "DEBUG"},
        "server": {"host": "127.0.0.1", "port": 8765},
        "sync": {"watcher": {"enabled": True}, "scheduler": {"enabled": False}},
    },
    "team": {
        "pheasant": {"environment": "team", "log_level": "INFO"},
        "server": {
            "host": "0.0.0.0",
            "port": 8765,
            "mcp": {"transports": {"stdio": False, "streamable_http": True, "sse": True}},
            "api": {"enabled": True, "openapi": False},
        },
    },
    "cloud-hybrid": {
        "pheasant": {"environment": "cloud-hybrid", "log_level": "INFO"},
        "server": {"host": "0.0.0.0", "port": 8765},
        "sync": {"watcher": {"enabled": True}, "scheduler": {"enabled": True}},
    },
}


def profile_data(name: str | None) -> dict[str, Any]:
    return PROFILES.get(name or "quickstart", PROFILES["quickstart"])


def profile_names() -> list[str]:
    return sorted(PROFILES)
