from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from syncsage.config.profiles import profile_data, profile_names
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


def load_layered_config(
    path: str | Path | None = None,
    profile: str | None = "quickstart",
    overrides: dict[str, Any] | None = None,
) -> SyncSageConfig:
    data = SyncSageConfig().model_dump(mode="json")
    data = deep_merge(data, profile_data(profile))
    if path is not None and Path(path).exists():
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("Config root must be a mapping")
        data = deep_merge(data, loaded)
    data = deep_merge(data, overrides or {})
    return SyncSageConfig.model_validate(data)


def effective_config_dict(
    path: str | Path | None = None,
    profile: str | None = "quickstart",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return load_layered_config(path, profile, overrides).model_dump(mode="json")


def render_init_config(profile: str = "quickstart") -> str:
    data = deep_merge(SyncSageConfig().model_dump(mode="json"), profile_data(profile))
    data["sources"] = []
    header = (
        f"# SyncSage {profile} profile configuration\n"
        f"# Available profiles: {', '.join(profile_names())}\n"
        "# Add sources under the sources: list when ready.\n"
    )
    rendered = yaml.safe_dump(data, sort_keys=False)
    rendered = rendered.replace("sources:\n\n", "sources: []\n")
    return header + rendered


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_override_pairs(pairs: list[str] | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigError(f"Override must use key=value syntax: {pair}")
        key, raw_value = pair.split("=", 1)
        cursor = data
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _parse_override_value(raw_value)
    return data


def _parse_override_value(raw_value: str) -> Any:
    lowered = raw_value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(raw_value)
    except ValueError:
        pass
    try:
        return float(raw_value)
    except ValueError:
        pass
    return raw_value


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
