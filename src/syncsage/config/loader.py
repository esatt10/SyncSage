from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from syncsage.config.schema import SyncSageConfig


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> SyncSageConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")
    return SyncSageConfig.model_validate(data)


def config_hash(config: SyncSageConfig | dict[str, Any]) -> str:
    payload = config.model_dump(mode="json") if isinstance(config, SyncSageConfig) else config
    text = yaml.safe_dump(payload, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_source_paths(config: SyncSageConfig, require_exists: bool = False) -> list[str]:
    errors: list[str] = []
    for source in config.sources:
        if require_exists and not source.path.exists():
            errors.append(f"source {source.name} path does not exist: {source.path}")
    return errors
