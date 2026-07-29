"""Product Framework Step 31.2 — Notion connector (first-party SDK plugin).

Fully offline against recorded API fixtures (``tests/fixtures/notion/``):
deterministic block→Markdown rendering, cursor pagination, engine e2e with
an idempotent second sync, per-page incremental skip (``ItemNotModified``),
ACL-capture metadata reserved for Phase 32, conformance-harness pass, and
the pyproject entry-point wiring guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncsage.config.loader import load_config
from syncsage.config.schema import PluginSourceType, SourceConfig
from syncsage.connectors.notion import NotionConnector
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.sync import connector_registry
from syncsage.sync.connectors import ConnectorUnavailable, ItemNotModified
from syncsage.sync.engine import SyncEngine
from syncsage.testing import ConnectorConformance

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "notion"
PAGE_A = "aaaa1111-0000-0000-0000-000000000001"
PAGE_B = "bbbb2222-0000-0000-0000-000000000002"


class FakeNotionAPI:
    """Fixture-backed stand-in for ``_notion_request`` (cursor pagination)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.edits: dict[str, str] = {}  # page_id -> overridden last_edited_time
        self.extra_blocks: dict[str, str] = {}  # page_id -> appended paragraph

    def _load(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def __call__(self, url: str, token: str, timeout: int, payload=None) -> dict:
        assert token == "secret-test-token"
        self.calls.append(url)
        if url.endswith("/search"):
            page = "search_page2.json" if payload and payload.get("start_cursor") else "search.json"
            doc = self._load(page)
            for result in doc["results"]:
                if result.get("id") in self.edits:
                    result["last_edited_time"] = self.edits[result["id"]]
            return doc
        if "/blocks/" in url:
            block_id = url.split("/blocks/")[1].split("/children")[0]
            name = f"blocks_{block_id.split('-0000')[0]}.json"
            fixture = FIXTURES / name
            if not fixture.exists():
                fixture = FIXTURES / f"blocks_{block_id}.json"
            doc = json.loads(fixture.read_text(encoding="utf-8"))
            if block_id in self.extra_blocks:
                doc["results"].append(
                    {
                        "object": "block",
                        "id": f"blk-extra-{block_id[:8]}",
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": [{"plain_text": self.extra_blocks[block_id]}]},
                    }
                )
            return doc
        raise AssertionError(f"unexpected Notion URL: {url}")


@pytest.fixture()
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeNotionAPI:
    api = FakeNotionAPI()
    monkeypatch.setattr("syncsage.connectors.notion._notion_request", api)
    monkeypatch.setenv("NOTION_TOKEN", "secret-test-token")
    return api


def _source(**overrides) -> SourceConfig:
    return SourceConfig(
        name=overrides.pop("name", "team-notion"),
        type=PluginSourceType("notion"),
        path=Path("/unused"),
        include=[],
        **overrides,
    )


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.db")
    state.migrate()
    return state


def test_lists_pages_across_cursor_pages_with_acl_metadata(
    tmp_path: Path, fake_api: FakeNotionAPI
) -> None:
    connector = NotionConnector(_source(), _state(tmp_path))
    items = connector.list_items()
    assert [i.metadata["page_id"] for i in items] == [PAGE_A, PAGE_B]  # database ignored
    assert sum("/search" in url for url in fake_api.calls) == 2  # both cursor pages
    runbook = items[0]
    assert runbook.relative_path == "deploy-runbook-aaaa1111.md"
    assert runbook.metadata["acl"] == {"created_by": "user-ada", "last_edited_by": "user-curie"}
    assert runbook.sha256 and runbook.etag == "2026-07-10T09:00:00.000Z"


def test_rendering_is_deterministic_markdown(tmp_path: Path, fake_api: FakeNotionAPI) -> None:
    connector = NotionConnector(_source(), _state(tmp_path))
    item = connector.list_items()[0]
    first = connector.read_item(item)
    second = connector.read_item(item)
    assert first.content == second.content
    text = first.content.decode("utf-8")
    assert "# Deploy Runbook" in text
    assert "## Rollback procedure" in text
    assert "Freeze deploys, then run the rollback script from the ops box." in text
    assert "- [x] Page the on-call" in text
    assert "- Verification steps" in text
    assert "  - Check /health returns 200" in text  # nested child block
    assert "```bash" in text and "kubectl rollout undo deploy/api" in text


def test_missing_token_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    connector = NotionConnector(_source(), _state(tmp_path))
    with pytest.raises(ConnectorUnavailable, match="NOTION_TOKEN"):
        connector.list_items()


def test_incremental_read_skips_unchanged_pages(tmp_path: Path, fake_api: FakeNotionAPI) -> None:
    connector = NotionConnector(_source(), _state(tmp_path))
    items = connector.list_items()
    cursor, watermark = connector.checkpoint_from_items(items)
    assert cursor["page_edits"][PAGE_B] == "2026-07-12T15:30:00.000Z"
    assert watermark["max_last_edited"] == "2026-07-12T15:30:00.000Z"
    connector.set_checkpoint(cursor, watermark, "healthy")

    connector.begin_sync("incremental")
    with pytest.raises(ItemNotModified):
        connector.read_item(items[0])

    # A page edited since the checkpoint is re-read (no skip).
    fake_api.edits[PAGE_A] = "2026-07-14T08:00:00.000Z"
    fresh = connector.list_items()
    payload = connector.read_item(fresh[0])
    assert b"Deploy Runbook" in payload.content

    # full mode never consults the checkpoint.
    connector.begin_sync("full")
    assert connector.read_item(items[0]).content


def test_engine_e2e_idempotent_and_incremental(tmp_path: Path, fake_api: FakeNotionAPI) -> None:
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("notion", NotionConnector)
    try:
        config_path = tmp_path / "syncsage.yaml"
        config_path.write_text(
            f"""syncsage:
  name: notion-test
  state_path: {tmp_path / "state"}
  vault_path: {tmp_path / "vault"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sources:
  - name: team-notion
    type: notion
    path: /unused
    include: []
""",
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        paths = StatePaths.from_config(cfg)
        paths.ensure()
        state = StateStore(paths.sqlite)
        state.migrate()
        engine = SyncEngine(cfg, paths, state)
        try:
            first = engine.sync_source("team-notion", "incremental")
            assert first.indexed_artifacts == 2

            block_calls_before = sum("/blocks/" in u for u in fake_api.calls)
            second = engine.sync_source("team-notion", "incremental")
            assert second.indexed_artifacts == 0
            assert second.skipped_artifacts == 2
            # Idempotent AND incremental: unchanged pages fetch no blocks.
            assert sum("/blocks/" in u for u in fake_api.calls) == block_calls_before

            # Edit-time bump with UNCHANGED content: the page is re-fetched
            # (validators say changed) but the content sha matches → no
            # re-index. The other page is skipped without any block fetch.
            fake_api.edits[PAGE_B] = "2026-07-15T10:00:00.000Z"
            a_blocks_before = sum(PAGE_A in u and "/blocks/" in u for u in fake_api.calls)
            third = engine.sync_source("team-notion", "incremental")
            assert third.indexed_artifacts == 0
            assert third.skipped_artifacts == 2
            assert sum(PAGE_A in u and "/blocks/" in u for u in fake_api.calls) == a_blocks_before

            # A real content edit → exactly that page re-indexed.
            fake_api.edits[PAGE_B] = "2026-07-16T11:00:00.000Z"
            fake_api.extra_blocks[PAGE_B] = "New paragraph from the latest edit."
            fourth = engine.sync_source("team-notion", "incremental")
            assert fourth.indexed_artifacts == 1
            assert fourth.skipped_artifacts == 1
        finally:
            engine.close()
    finally:
        connector_registry.reset_connector_registry()


def test_pyproject_declares_the_entry_point() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    assert (
        data["project"]["entry-points"]["syncsage.connectors"]["notion"]
        == "syncsage.connectors.notion:NotionConnector"
    )


class TestNotionConnectorConformance(ConnectorConformance):
    @pytest.fixture(autouse=True)
    def _wire(self, fake_api: FakeNotionAPI) -> None:
        self.api = fake_api

    def make_connector(self, tmp_path: Path, state: StateStore) -> NotionConnector:
        return NotionConnector(_source(), state)
