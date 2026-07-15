"""Step 31.1 — the public conformance bar, applied to us first.

The built-in FilesystemConnector and the canonical example plugin
(``tests/fixtures/syncsage-connector-example``) both pass the exact
``syncsage.testing.ConnectorConformance`` harness a third-party package
would subclass.
"""

from __future__ import annotations

import sys
from pathlib import Path

from syncsage.config.schema import PluginSourceType, SourceConfig, SourceType
from syncsage.persistence.state_store import StateStore
from syncsage.sync.connectors import FilesystemConnector, SourceConnector
from syncsage.testing import ConnectorConformance

EXAMPLE_PKG = Path(__file__).resolve().parent / "fixtures" / "syncsage-connector-example"


def _sample_content(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    root.mkdir(exist_ok=True)
    (root / "notes.txt").write_text("A note about conformance.", encoding="utf-8")
    (root / "guide.txt").write_text("A guide to connectors.", encoding="utf-8")
    return root


class TestFilesystemConnectorConformance(ConnectorConformance):
    def make_connector(self, tmp_path: Path, state: StateStore) -> SourceConnector:
        source = SourceConfig(
            name="fs-conformance",
            type=SourceType.document_folder,
            path=_sample_content(tmp_path),
            include=["**/*.txt"],
        )
        return FilesystemConnector(source, state)


class TestExampleConnectorConformance(ConnectorConformance):
    def make_connector(self, tmp_path: Path, state: StateStore) -> SourceConnector:
        sys.path.insert(0, str(EXAMPLE_PKG))
        try:
            from syncsage_connector_example import StaticDirConnector
        finally:
            sys.path.remove(str(EXAMPLE_PKG))
        source = SourceConfig(
            name="staticdir-conformance",
            type=PluginSourceType("staticdir"),
            path=_sample_content(tmp_path),
        )
        return StaticDirConnector(source, state)
