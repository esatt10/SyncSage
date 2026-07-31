"""First-run bootstrap for ``syncsage up`` (Product Framework Step 30.1).

One command takes a directory from nothing to an indexed, queryable
knowledge base: detect what the directory is, generate a laptop-shaped
config if none exists, then hand off to the ordinary sync engine and
server. Everything here is config plumbing — indexing and serving reuse
the exact code paths behind ``syncsage sync`` and ``syncsage start``.

Generated configs anchor all state under ``.syncsage/{state,vault,exports}``
next to the config file (absolute paths, so later invocations from another
working directory keep hitting the same state). An existing config file is
never rewritten — re-running ``up`` reuses it unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

try:  # the dependency-light shim exposes the same predicate it dumps with
    from yaml import _needs_quotes  # type: ignore[attr-defined]
except ImportError:  # PyYAML

    def _needs_quotes(text: str) -> bool:
        return ":" in text or text[:1] in tuple("*&!%@`[{|>-?#,'\"") or text != text.strip()


from syncsage.config.loader import deep_merge
from syncsage.config.profiles import profile_data
from syncsage.config.schema import SourceType, SyncSageConfig

STATE_DIRNAME = ".syncsage"


def _scrub(obj: Any) -> Any:
    """Drop ``None`` values and empty containers from a config dict.

    ``model_validate`` refills every dropped key with its default, and the
    dependency-light ``yaml`` shim used in offline test environments cannot
    round-trip ``None`` (it reloads as the string ``"None"``) or empty
    blocks — so the generated file simply omits them.
    """
    if isinstance(obj, dict):
        cleaned = {k: _scrub(v) for k, v in obj.items() if v is not None}
        return {k: v for k, v in cleaned.items() if v not in ({}, [])}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj if v is not None]
    return obj


def _render_sources_block(entries: list[dict[str, Any]]) -> str:
    """Render ``sources:`` by hand in the inline-first-key list style.

    The yaml shim's ``safe_dump`` writes list-of-dict items as a bare ``-``
    line it cannot itself re-read; both it and PyYAML parse this shape.
    List values (``include``, ``urls``) render as nested block sequences,
    which both parsers also handle.
    """
    lines = ["sources:"]
    for entry in entries:
        prefix = "  - "
        for key, value in entry.items():
            if isinstance(value, list):
                lines.append(f"{prefix}{key}:")
                prefix = "    "
                for item in value:
                    lines.append(f"      - {_quote(item)}")
                continue
            if isinstance(value, dict):
                lines.append(f"{prefix}{key}:")
                prefix = "    "
                for sub_key, sub_value in value.items():
                    lines.append(f"      {sub_key}: {_quote(sub_value)}")
                continue
            lines.append(f"{prefix}{key}: {_quote(value)}")
            prefix = "    "
    return "\n".join(lines) + "\n"


def _quote(value: Any) -> str:
    """Quote a scalar when YAML would otherwise mis-read it.

    Glob patterns start with ``*`` (an alias anchor to YAML) and URLs carry
    a ``:`` — both need quoting; plain words must stay bare so the existing
    generated configs are unchanged.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "" or _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def detect_source_type(path: Path) -> SourceType:
    """Classify a local directory for the generated source entry."""
    if (path / ".obsidian").is_dir():
        return SourceType.obsidian_vault
    if (path / ".git").is_dir():
        return SourceType.repository
    return SourceType.document_folder


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


def state_root(config_path: Path) -> Path:
    """Where a generated config anchors its state/vault/exports/clones."""
    return (config_path.resolve().parent / STATE_DIRNAME).resolve()


def _common_workspace(paths: list[Path], fallback: Path) -> Path:
    """Deepest directory containing every local target.

    ``workspace_root`` anchors relative source paths, so it has to be an
    ancestor of all of them; with nothing local (a pure web/connector
    config) it falls back to the config's own directory.
    """
    directories = [p if p.is_dir() else p.parent for p in paths]
    if not directories:
        return fallback
    if len(directories) == 1:
        return directories[0]
    try:
        import os.path

        return Path(os.path.commonpath([str(d) for d in directories]))
    except ValueError:  # different drives on Windows
        return fallback


def render_up_config(
    target: Path | str | list,
    config_path: Path,
    *,
    name: str | None = None,
    port: int = 8765,
    profile: str = "quickstart",
) -> str:
    """Render the YAML for a laptop config over one or more targets.

    ``target`` accepts a plain path (the original single-source form, kept
    for callers and tests that predate multi-target ``up``) or a list of
    ``ResolvedTarget``s from :mod:`syncsage.targets`.

    Deterministic for a given (targets, config_path, options) tuple so a
    regenerated config is byte-identical — the idempotency bar every
    bootstrap command in this codebase has to clear.
    """
    from syncsage.targets import ResolvedTarget

    config_path = Path(config_path)
    local = state_root(config_path)

    if isinstance(target, (str, Path)):
        resolved = Path(target).resolve()
        source_type = detect_source_type(resolved)
        targets = [
            ResolvedTarget(
                name=name or slugify(resolved.name),
                type=source_type.value,
                path=str(resolved),
                description=f"Auto-detected {source_type.value}",
            )
        ]
    else:
        targets = list(target)
    if not targets:
        raise ValueError("at least one target is required")

    local_paths = [Path(t.path) for t in targets if t.local]
    workspace = _common_workspace(local_paths, config_path.resolve().parent)
    kb_name = name or (targets[0].name if len(targets) == 1 else slugify(workspace.name))

    data: dict[str, Any] = deep_merge(
        SyncSageConfig().model_dump(mode="json"), profile_data(profile)
    )
    origin = targets[0].path if len(targets) == 1 else f"{len(targets)} sources"
    data["syncsage"].update(
        {
            "name": kb_name,
            "description": f"Personal knowledge base over {origin}",
            "state_path": str(local / "state"),
            "vault_path": str(local / "vault"),
            "exports_path": str(local / "exports"),
            "workspace_root": str(workspace),
        }
    )
    data["server"]["port"] = port
    roots = [str(workspace)]
    for candidate in [*(str(p) for p in local_paths), str(local / "vault"), str(local / "exports")]:
        if candidate not in roots:
            roots.append(candidate)
    data["security"]["allow_workspace_roots"] = roots
    data.pop("sources", None)
    sources_block = _render_sources_block([t.to_source_dict() for t in targets])

    header_lines = [f"# Generated by `syncsage up` ({len(targets)} source(s))"]
    for entry in targets:
        header_lines.append(f"#   {entry.name}: {entry.type} <- {entry.path}")
    header_lines.append("# Edit freely — `syncsage up` never overwrites an existing config.")
    header = "\n".join(header_lines) + "\n"
    return header + yaml.safe_dump(_scrub(data), sort_keys=False) + sources_block


def ensure_up_config(
    target: Path | str | list,
    config_path: Path,
    *,
    name: str | None = None,
    port: int = 8765,
    profile: str = "quickstart",
) -> bool:
    """Write the generated config unless one already exists.

    Returns True when a new config was written, False when an existing
    file was left untouched (the reuse path).
    """
    config_path = Path(config_path)
    if config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        render_up_config(target, config_path, name=name, port=port, profile=profile),
        encoding="utf-8",
    )
    return True
