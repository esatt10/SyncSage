from __future__ import annotations

import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncsage.api.app import create_app
from syncsage.config.schema import (
    SourceConfig,
    SourceConnectorSettings,
    SourceSyncSettings,
    SourceType,
    SyncSageConfig,
    SyncSageSettings,
)
from syncsage.mcp_server.tools import SyncSageTools
from syncsage.sync.engine import SyncEngine


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def web_server(tmp_path: Path) -> Iterator[str]:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "guide.md").write_text(
        "# Connector Guide\n\nWeb collection connector content for search.\n",
        encoding="utf-8",
    )
    handler = partial(QuietHandler, directory=str(web_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_validate_only_does_not_write_index_state(tmp_path: Path, workspace_copy: Path) -> None:
    config = _config(
        tmp_path,
        SourceConfig(
            name="sample-repo",
            type=SourceType.repository,
            path=workspace_copy / "syncsage-repo",
            sync=SourceSyncSettings(on_startup=False),
        ),
    )
    engine = SyncEngine(config)

    result = engine.sync_source("sample-repo", "validate_only")

    assert result.status == "validated"
    assert engine.stats["artifact_count"] == 0
    assert engine.stats["chunk_count"] == 0
    assert not engine.manifests.path_for("sample-repo").exists()


def test_filesystem_source_respects_max_depth(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    nested = root / "section" / "deep"
    nested.mkdir(parents=True)
    (root / "root.md").write_text("# Root\n", encoding="utf-8")
    (root / "section" / "child.md").write_text("# Child\n", encoding="utf-8")
    (nested / "deep.md").write_text("# Deep\n", encoding="utf-8")
    config = _config(
        tmp_path,
        SourceConfig(
            name="depth-limited",
            type=SourceType.document_folder,
            path=root,
            include=["**/*.md"],
            max_depth=1,
            sync=SourceSyncSettings(on_startup=False),
        ),
    )
    engine = SyncEngine(config)

    result = engine.sync_source("depth-limited", "full")

    assert result.indexed_artifacts == 2
    paths = {
        row["relative_path"]
        for row in engine.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id=?",
            ("depth-limited",),
        )
    }
    assert paths == {"root.md", "section/child.md"}


def test_web_collection_connector_syncs_and_exposes_checkpoint(
    tmp_path: Path,
    web_server: str,
) -> None:
    config = _config(
        tmp_path,
        SourceConfig(
            name="web-docs",
            type=SourceType.web_collection,
            path=tmp_path,
            urls=[f"{web_server}/guide.md"],
            include=["**/*.md"],
            sync=SourceSyncSettings(on_startup=False),
            connector=SourceConnectorSettings(allow_experimental=True),
        ),
    )
    engine = SyncEngine(config)

    result = engine.sync_source("web-docs", "full")

    assert result.indexed_artifacts == 1
    assert result.details["connector_type"] == "web_collection"
    rows = engine.state.rows(
        "SELECT relative_path, path FROM artifacts WHERE source_id=?",
        ("web-docs",),
    )
    assert len(rows) == 1
    assert rows[0]["relative_path"].endswith("/guide.md")
    assert rows[0]["path"].startswith("http://127.0.0.1:")
    assert engine.search_context("connector", max_results=5)["results"]

    checkpoint = engine.state.get_source_checkpoint("web-docs")
    assert checkpoint is not None
    assert checkpoint["connector_type"] == "web_collection"
    assert checkpoint["high_watermark"]["item_count"] == 1

    api_status = TestClient(create_app(config)).get("/sync/status").json()
    mcp_status = SyncSageTools(config).get_sync_status(config.knowledge_base_id)

    assert api_status["checkpoints"][0]["source_id"] == "web-docs"
    assert mcp_status["checkpoints"][0]["source_id"] == "web-docs"


def _config(tmp_path: Path, source: SourceConfig) -> SyncSageConfig:
    return SyncSageConfig(
        syncsage=SyncSageSettings(
            name="connector-tests",
            state_path=tmp_path / "state",
            vault_path=tmp_path / "vault",
            workspace_root=tmp_path,
            exports_path=tmp_path / "exports",
        ),
        sources=[source],
    )
