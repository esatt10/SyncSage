"""Connector plugin registry (Product Framework Step 31.1).

Third-party packages add a source type to SyncSage without forking it by
declaring an entry point in the ``syncsage.connectors`` group::

    [project.entry-points."syncsage.connectors"]
    staticdir = "my_pkg.connectors:StaticDirConnector"

The entry-point name is the ``sources[].type`` string users put in
``syncsage.yaml``. Embedders and tests can also register classes
programmatically with :func:`register_connector_class`.

Built-in connectors are NOT routed through this registry —
``connectors.connector_for_source`` consults it only for non-built-in
types, so an installation with no plugins behaves byte-identically to
pre-31.1. The canonical third-party package shape lives at
``tests/fixtures/syncsage-connector-example/``; the public quality bar is
``syncsage.testing.ConnectorConformance``.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "syncsage.connectors"

_programmatic: dict[str, type] = {}
_entry_point_cache: dict[str, type] | None = None


def _is_connector_class(obj: object) -> bool:
    from syncsage.sync.connectors import SourceConnector

    return isinstance(obj, type) and issubclass(obj, SourceConnector)


def register_connector_class(type_name: str, connector_class: type) -> None:
    """Register a connector class for a source-type name (embedders/tests).

    Programmatic registrations take precedence over entry points of the
    same name so a test or embedding application can always pin behavior.
    """
    if not type_name or not isinstance(type_name, str):
        raise ValueError("connector type_name must be a non-empty string")
    if not _is_connector_class(connector_class):
        raise TypeError(
            f"{connector_class!r} is not a SourceConnector subclass; "
            "connector plugins must subclass syncsage.sync.connectors.SourceConnector"
        )
    _programmatic[type_name] = connector_class


def _load_entry_points() -> dict[str, type]:
    global _entry_point_cache
    if _entry_point_cache is None:
        loaded: dict[str, type] = {}
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
            except Exception:  # a broken plugin must not take down sync
                logger.warning("connector plugin %r failed to import; skipping", ep.name)
                continue
            if not _is_connector_class(obj):
                logger.warning(
                    "connector plugin %r is not a SourceConnector subclass; skipping", ep.name
                )
                continue
            loaded[ep.name] = obj
        _entry_point_cache = loaded
    return _entry_point_cache


def get_connector_class(type_name: str) -> type | None:
    """Resolve a plugin connector class for a source-type name, or None."""
    if type_name in _programmatic:
        return _programmatic[type_name]
    return _load_entry_points().get(type_name)


def list_connector_types() -> list[str]:
    """Every plugin source type currently resolvable (sorted)."""
    return sorted(set(_load_entry_points()) | set(_programmatic))


def reset_connector_registry() -> None:
    """Drop programmatic registrations and the entry-point cache (tests)."""
    global _entry_point_cache
    _programmatic.clear()
    _entry_point_cache = None
