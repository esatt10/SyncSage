"""Product Framework Step 33.1 — agent memory as a region.

Acceptance (SR docs/PRODUCT_FRAMEWORK.md §3c): store determinism +
append-only idempotency; engine e2e write → sync → searchable with a
zero-work second sync; MCP facade read-your-writes + actionable
no-source error; HTTP POST/GET round-trip + 400 without a memory source;
publisher advertises the ``memory`` modality (wire format unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from syncsage.config.loader import load_config
from syncsage.mcp_server.tools import SyncSageTools
from syncsage.memory.store import MEMORY_RECORD_VERSION, MemoryRecord, MemoryStore, memory_source

PINNED_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _write_config(tmp_path: Path, *, include_memory: bool = True) -> Path:
    memory_block = (
        """  - name: agent-memory
    type: memory
    path: memory
"""
        if include_memory
        else ""
    )
    config_path = tmp_path / "syncsage.yaml"
    config_path.write_text(
        f"""syncsage:
  name: memory-test
  state_path: {tmp_path / ".syncsage" / "state"}
  vault_path: {tmp_path / ".syncsage" / "vault"}
  exports_path: {tmp_path / ".syncsage" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: notes
    type: document_folder
    path: docs
{memory_block}""",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "readme.md").write_text("# Plain notes\n", encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    return config_path


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


def test_append_is_deterministic_and_append_only(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    first, created_first = store.append(
        "The staging cluster lives in us-east-2.",
        scope="org",
        subject="infra",
        tags=["deploy", "aws"],
        now=PINNED_NOW,
    )
    assert created_first
    assert first.record_id.startswith("mem-20260716T120000Z-")
    assert first.path.exists()

    second, created_second = store.append(
        "The staging cluster lives in us-east-2.",
        scope="org",
        subject="infra",
        tags=["deploy", "aws"],
        now=PINNED_NOW,
    )
    assert not created_second
    assert second.record_id == first.record_id
    assert second.text == first.text

    raw = first.path.read_text(encoding="utf-8")
    assert f"schema_version: {MEMORY_RECORD_VERSION}" in raw
    assert "memory_scope: org" in raw
    assert raw.rstrip().endswith("us-east-2.")


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    record, _ = store.append(
        "Alice prefers dark mode.",
        scope="user",
        subject="alice",
        supersedes="mem-20260101T000000Z-deadbeef00000000",
        tags=["prefs"],
        now=PINNED_NOW,
    )
    loaded = store.load(record.path)
    assert isinstance(loaded, MemoryRecord)
    assert loaded.record_id == record.record_id
    assert loaded.scope == "user"
    assert loaded.subject == "alice"
    assert loaded.supersedes == "mem-20260101T000000Z-deadbeef00000000"
    assert loaded.tags == ("prefs",)
    assert loaded.text == "Alice prefers dark mode."
    assert loaded.asserted_at == "2026-07-16T12:00:00Z"


def test_scope_and_text_validation(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError, match="scope"):
        store.append("something", scope="galaxy")
    with pytest.raises(ValueError, match="non-empty"):
        store.append("   ")
    with pytest.raises(ValueError, match="scope"):
        store.list_records("galaxy")


def test_list_records_is_sorted_and_scope_filtered(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.append("fact one", scope="user", now=PINNED_NOW)
    store.append("fact two", scope="org", now=PINNED_NOW)
    assert len(store.list_records()) == 2
    org_only = store.list_records("org")
    assert [r.text for r in org_only] == ["fact two"]


# ---------------------------------------------------------------------------
# MCP facade: write → indexed → searchable (read-your-writes)
# ---------------------------------------------------------------------------


def test_memory_write_is_searchable_immediately_and_resync_is_zero_work(
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path))
    assert memory_source(config) is not None
    tools = SyncSageTools(config)
    try:
        result = tools.memory_write(
            "memory-test",
            "The quarterly retention experiment used cohort seventeen.",
            scope="org",
            subject="research",
        )
        assert result["created"] is True
        assert result["sync"]["indexed_artifacts"] == 1

        hits = tools.search_context("memory-test", "cohort seventeen retention", mode="text")
        flattened = str(hits).lower()
        assert "cohort seventeen" in flattened

        again = tools.memory_write(
            "memory-test",
            "The quarterly retention experiment used cohort seventeen.",
            scope="org",
            subject="research",
        )
        assert again["created"] is False
        assert "sync" not in again  # no re-index for an identical record
    finally:
        tools.engine.close()


def test_memory_write_without_memory_source_is_actionable(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, include_memory=False))
    tools = SyncSageTools(config)
    try:
        with pytest.raises(ValueError, match="type: memory"):
            tools.memory_write("memory-test", "anything")
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_http_memory_write_and_list(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from syncsage.api.app import create_app

    config_path = _write_config(tmp_path)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        resp = client.post(
            "/memory",
            json={"text": "Deploy freezes start Friday 5pm.", "scope": "org", "sync": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["record"]["schema_version"] == MEMORY_RECORD_VERSION
        assert body["sync"]["indexed_artifacts"] == 1

        listing = client.get("/memory", params={"scope": "org"})
        assert listing.status_code == 200
        assert [r["text"] for r in listing.json()["records"]] == [
            "Deploy freezes start Friday 5pm."
        ]

        bad = client.post("/memory", json={"text": "", "scope": "org"})
        assert bad.status_code == 400
    finally:
        app.state.engine.close()


def test_http_memory_without_source_is_400(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from syncsage.api.app import create_app

    config_path = _write_config(tmp_path, include_memory=False)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        assert client.post("/memory", json={"text": "x"}).status_code == 400
        assert client.get("/memory").status_code == 400
    finally:
        app.state.engine.close()


# ---------------------------------------------------------------------------
# Publisher capability (routing prep for 33.3) — wire format unchanged
# ---------------------------------------------------------------------------


def test_publisher_advertises_memory_modality(tmp_path: Path) -> None:
    from syncsage.persistence.state_store import StateStore
    from syncsage.synapse.publisher import ContractPublisher

    state = StateStore(tmp_path / "pub-state.db")
    state.migrate()

    with_memory = load_config(_write_config(tmp_path))
    modalities = ContractPublisher(with_memory, state)._capabilities()["modalities"]
    assert "memory" in modalities

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    without = load_config(_write_config(plain_dir, include_memory=False))
    assert "memory" not in ContractPublisher(without, state)._capabilities()["modalities"]
