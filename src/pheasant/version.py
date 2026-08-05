from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

# The installable distribution name, which differs from the ``pheasant`` import
# root: ``pheasant`` is taken on PyPI by an unrelated project.
PACKAGE_NAME = "pheasant-kb"


def _version_from_pyproject() -> str | None:
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.exists():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = data.get("project", {}).get("version")
        return str(version) if version else None
    return None


def get_version() -> str:
    """Return the pheasant package version from the closest reliable source."""

    source_version = _version_from_pyproject()
    if source_version:
        return source_version
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "0+unknown"


__version__ = get_version()
