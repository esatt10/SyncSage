from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from pheasant.config.profiles import profile_data, profile_names
from pheasant.config.schema import PheasantConfig


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> PheasantConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")
    return PheasantConfig.model_validate(data)


def load_layered_config(
    path: str | Path | None = None,
    profile: str | None = "quickstart",
    overrides: dict[str, Any] | None = None,
) -> PheasantConfig:
    data = PheasantConfig().model_dump(mode="json")
    data = deep_merge(data, profile_data(profile))
    if path is not None and Path(path).exists():
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("Config root must be a mapping")
        data = deep_merge(data, loaded)
    data = deep_merge(data, overrides or {})
    return PheasantConfig.model_validate(data)


def effective_config_dict(
    path: str | Path | None = None,
    profile: str | None = "quickstart",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return load_layered_config(path, profile, overrides).model_dump(mode="json")


def render_init_config(profile: str = "quickstart") -> str:
    data = deep_merge(PheasantConfig().model_dump(mode="json"), profile_data(profile))
    data["sources"] = []
    header = (
        f"# pheasant {profile} profile configuration\n"
        f"# Available profiles: {', '.join(profile_names())}\n"
        "# Add sources under the sources: list when ready.\n"
    )
    rendered = dump_config_yaml(data)
    rendered = rendered.replace("sources:\n\n", "sources: []\n")
    return header + rendered


def dump_config_yaml(data: dict[str, Any]) -> str:
    """Dump a config mapping so **both** YAML readers can load it back.

    pheasant ships a dependency-light YAML shim (``yaml.py`` at the repo root)
    for installs without PyYAML, and it is stricter than PyYAML in two ways
    that a plain ``yaml.safe_dump`` walks straight into:

    1. **Sequences must be indented under their key.** PyYAML writes

       .. code-block:: yaml

           retrieval_modes:
           - text

       with the dash at the *parent's* indent; the shim requires ``+2`` and
       fails with ``AttributeError: 'dict' object has no attribute 'append'``.
       Every config ``pheasant up`` has ever written was unreadable by the
       shim for exactly this reason — found by round-tripping a real generated
       config through it rather than through whichever ``yaml`` the tests
       happened to import.

    2. **A list item containing ``:`` must be quoted**, or the shim splits it
       into a mapping — which silently turned every entry of
       ``cors_origins`` into ``{"http": "//localhost:5173"}``. Wrong data, no
       error, on the setting that decides which browser origins may drive an
       unauthenticated API.

    Both are fixed here, once, for every path that writes config YAML.
    """
    if not hasattr(yaml, "SafeDumper"):  # the shim: its own dumper is correct
        return yaml.safe_dump(data, sort_keys=False)

    class _IndentingDumper(yaml.SafeDumper):
        def increase_indent(self, flow=False, indentless=False):  # noqa: ARG002
            # `indentless=True` is what un-indents block sequences.
            return super().increase_indent(flow, False)

    def _represent_str(dumper, value):
        style = '"' if ":" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _IndentingDumper.add_representer(str, _represent_str)
    return yaml.dump(data, Dumper=_IndentingDumper, sort_keys=False, default_flow_style=False)


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


def config_hash(config: PheasantConfig | dict[str, Any]) -> str:
    payload = config.model_dump(mode="json") if isinstance(config, PheasantConfig) else config
    text = yaml.safe_dump(payload, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_source_paths(config: PheasantConfig, require_exists: bool = False) -> list[str]:
    errors: list[str] = []
    for source in config.sources:
        if require_exists and not source.path.exists():
            errors.append(f"source {source.name} path does not exist: {source.path}")
    return errors
