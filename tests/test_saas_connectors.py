"""Product Framework Steps 31.3–31.6 — Google Drive / Slack / Confluence / IMAP.

All four ride the 31.1 SDK exactly like the Notion connector (31.2):
fixture-backed fakes at each module's single network touchpoint, so every
test is offline and deterministic. Shared acceptance per connector:
listing with ACL capture, deterministic rendering, per-item incremental
skip, engine e2e with an idempotent second sync, a ConnectorConformance
pass, and the pyproject entry-point guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.loader import load_config
from pheasant.config.schema import PluginSourceType, SourceConfig
from pheasant.connectors.confluence import ConfluenceConnector
from pheasant.connectors.gdrive import GDriveConnector
from pheasant.connectors.imap import ImapConnector
from pheasant.connectors.slack import SlackConnector
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.sync import connector_registry
from pheasant.sync.connectors import ItemNotModified
from pheasant.sync.engine import SyncEngine
from pheasant.testing import ConnectorConformance


def _source(type_name: str, **overrides) -> SourceConfig:
    return SourceConfig(
        name=overrides.pop("name", f"team-{type_name}"),
        type=PluginSourceType(type_name),
        path=overrides.pop("path", Path("/unused")),
        include=[],
        **overrides,
    )


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.db")
    state.migrate()
    return state


def _engine_sync_twice(
    tmp_path: Path,
    type_name: str,
    connector_class,
    expected: int,
    *,
    endpoint: str = "https://example.test",
    source_path: str = "/unused",
    expected_second_skip: int | None = None,
) -> None:
    """Engine e2e: index `expected` artifacts, then an idempotent second sync."""
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class(type_name, connector_class)
    try:
        config_path = tmp_path / "pheasant.yaml"
        config_path.write_text(
            f"""pheasant:
  name: {type_name}-test
  state_path: {tmp_path / "state-dir"}
  vault_path: {tmp_path / "vault"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sources:
  - name: team-{type_name}
    type: {type_name}
    path: {source_path}
    include: []
    connector:
      api_endpoint: {endpoint}
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
            first = engine.sync_source(f"team-{type_name}", "incremental")
            assert first.indexed_artifacts == expected
            second = engine.sync_source(f"team-{type_name}", "incremental")
            assert second.indexed_artifacts == 0
            skip = expected if expected_second_skip is None else expected_second_skip
            assert second.skipped_artifacts == skip
        finally:
            engine.close()
    finally:
        connector_registry.reset_connector_registry()


def test_pyproject_declares_all_first_party_connectors() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["pheasant.connectors"]
    assert eps == {
        "notion": "pheasant.connectors.notion:NotionConnector",
        "gdrive": "pheasant.connectors.gdrive:GDriveConnector",
        "slack": "pheasant.connectors.slack:SlackConnector",
        "confluence": "pheasant.connectors.confluence:ConfluenceConnector",
        "imap": "pheasant.connectors.imap:ImapConnector",
    }


# ---------------------------------------------------------------------------
# Google Drive (31.3)
# ---------------------------------------------------------------------------

GDRIVE_FILES = {
    "files": [
        {
            "id": "doc-alpha",
            "name": "Team Charter",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-07-10T09:00:00Z",
            "shared": True,
            "owners": [{"emailAddress": "ada@example.com"}],
            "webViewLink": "https://docs.google.com/doc-alpha",
        },
        {
            "id": "txt-beta",
            "name": "notes.txt",
            "mimeType": "text/plain",
            "modifiedTime": "2026-07-11T10:00:00Z",
            "md5Checksum": "cafe01",
            "owners": [{"emailAddress": "curie@example.com"}],
        },
        {"id": "img-gamma", "name": "logo.png", "mimeType": "image/png"},
    ]
}
GDRIVE_BODIES = {
    "doc-alpha": b"Charter: we ship small and often.",
    "txt-beta": b"remember the retro on friday",
}


@pytest.fixture()
def fake_gdrive(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(url: str, token: str, timeout: int, *, raw: bool = False):
        assert token == "gdrive-token"
        calls.append(url)
        if "/files?" in url:
            return json.loads(json.dumps(GDRIVE_FILES))
        for file_id, body in GDRIVE_BODIES.items():
            if f"/files/{file_id}" in url:
                return body
        raise AssertionError(f"unexpected Drive URL: {url}")

    monkeypatch.setattr("pheasant.connectors.gdrive._gdrive_request", fake)
    monkeypatch.setenv("GDRIVE_TOKEN", "gdrive-token")
    return calls


def test_gdrive_lists_text_files_with_acl_and_renders(tmp_path: Path, fake_gdrive) -> None:
    connector = GDriveConnector(_source("gdrive"), _state(tmp_path))
    items = connector.list_items()
    assert [i.metadata["file_id"] for i in items] == ["doc-alpha", "txt-beta"]  # png filtered
    assert items[0].metadata["acl"] == {"owners": ["ada@example.com"], "shared": True}
    payload = connector.read_item(items[0])
    assert payload.content == b"# Team Charter\n\nCharter: we ship small and often."
    assert connector.read_item(items[0]).content == payload.content  # deterministic

    cursor, watermark = connector.checkpoint_from_items(items)
    connector.set_checkpoint(cursor, watermark, "healthy")
    connector.begin_sync("incremental")
    with pytest.raises(ItemNotModified):
        connector.read_item(items[0])


def test_gdrive_engine_e2e(tmp_path: Path, fake_gdrive) -> None:
    _engine_sync_twice(tmp_path, "gdrive", GDriveConnector, expected=2)


class TestGDriveConformance(ConnectorConformance):
    @pytest.fixture(autouse=True)
    def _wire(self, fake_gdrive) -> None:
        pass

    def make_connector(self, tmp_path: Path, state: StateStore) -> GDriveConnector:
        return GDriveConnector(_source("gdrive"), state)


# ---------------------------------------------------------------------------
# Slack (31.4)
# ---------------------------------------------------------------------------

SLACK_CHANNELS = {
    "ok": True,
    "channels": [
        {
            "id": "C001",
            "name": "deploys",
            "is_private": False,
            "is_shared": True,
            "latest": {"ts": "1752600000.000100"},
        },
        {"id": "C002", "name": "archived", "is_archived": True},
    ],
    "response_metadata": {"next_cursor": ""},
}
SLACK_HISTORY = {
    "ok": True,
    "messages": [
        {"ts": "1752600000.000100", "user": "U42", "text": "rollback complete, all green"},
        {"ts": "1752590000.000050", "user": "U17", "text": "starting the rollback now"},
        {"ts": "1752580000.000010", "subtype": "channel_join", "user": "U99", "text": "joined"},
    ],
    "has_more": False,
}


@pytest.fixture()
def fake_slack(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(url: str, token: str, timeout: int):
        assert token == "xoxb-test"
        calls.append(url)
        if "conversations.list" in url:
            return json.loads(json.dumps(SLACK_CHANNELS))
        if "conversations.history" in url:
            return json.loads(json.dumps(SLACK_HISTORY))
        raise AssertionError(f"unexpected Slack URL: {url}")

    monkeypatch.setattr("pheasant.connectors.slack._slack_request", fake)
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-test")
    return calls


def test_slack_transcript_is_deterministic_and_incremental(tmp_path: Path, fake_slack) -> None:
    connector = SlackConnector(_source("slack"), _state(tmp_path))
    items = connector.list_items()
    assert [i.metadata["channel_id"] for i in items] == ["C001"]  # archived filtered
    assert items[0].metadata["acl"] == {"is_private": False, "is_shared": True}
    payload = connector.read_item(items[0])
    text = payload.content.decode("utf-8")
    # ts-ordered, joins filtered, ids rendered as-is.
    assert text.index("starting the rollback") < text.index("rollback complete")
    assert "joined" not in text
    assert connector.read_item(items[0]).content == payload.content

    cursor, watermark = connector.checkpoint_from_items(items)
    connector.set_checkpoint(cursor, watermark, "healthy")
    connector.begin_sync("incremental")
    with pytest.raises(ItemNotModified):
        connector.read_item(items[0])


def test_slack_engine_e2e(tmp_path: Path, fake_slack) -> None:
    _engine_sync_twice(tmp_path, "slack", SlackConnector, expected=1)


class TestSlackConformance(ConnectorConformance):
    @pytest.fixture(autouse=True)
    def _wire(self, fake_slack) -> None:
        pass

    def make_connector(self, tmp_path: Path, state: StateStore) -> SlackConnector:
        return SlackConnector(_source("slack"), state)


# ---------------------------------------------------------------------------
# Confluence (31.5)
# ---------------------------------------------------------------------------

CONFLUENCE_PAGES = {
    "results": [
        {
            "id": "9001",
            "title": "Incident Process",
            "version": {"number": 3, "when": "2026-07-09T12:00:00Z"},
            "space": {"key": "OPS"},
            "history": {"createdBy": {"accountId": "acct-ada"}},
            "body": {
                "storage": {
                    "value": (
                        "<h1>Severity levels</h1><p>Sev1 pages the on-call.</p>"
                        "<ul><li>Declare in #incidents</li></ul>"
                        "<pre>page oncall --sev 1</pre>"
                    )
                }
            },
        }
    ],
    "size": 1,
}


@pytest.fixture()
def fake_confluence(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(url: str, token: str, timeout: int):
        assert token == "conf-token"
        calls.append(url)
        return json.loads(json.dumps(CONFLUENCE_PAGES))

    monkeypatch.setattr("pheasant.connectors.confluence._confluence_request", fake)
    monkeypatch.setenv("CONFLUENCE_TOKEN", "conf-token")
    return calls


def _confluence_source() -> SourceConfig:
    source = _source("confluence")
    source.connector.api_endpoint = "https://example.test/wiki"
    return source


def test_confluence_storage_body_renders_deterministically(tmp_path: Path, fake_confluence) -> None:
    connector = ConfluenceConnector(_confluence_source(), _state(tmp_path))
    items = connector.list_items()
    assert items[0].metadata["acl"] == {"space": "OPS", "created_by": "acct-ada"}
    payload = connector.read_item(items[0])
    text = payload.content.decode("utf-8")
    assert "# Incident Process" in text
    assert "# Severity levels" in text
    assert "Sev1 pages the on-call." in text
    assert "- Declare in #incidents" in text
    assert "page oncall --sev 1" in text
    assert "storage_body" not in payload.metadata  # raw XHTML not persisted
    assert connector.read_item(items[0]).content == payload.content

    cursor, watermark = connector.checkpoint_from_items(items)
    connector.set_checkpoint(cursor, watermark, "healthy")
    connector.begin_sync("incremental")
    with pytest.raises(ItemNotModified):
        connector.read_item(items[0])


def test_confluence_engine_e2e(tmp_path: Path, fake_confluence) -> None:
    _engine_sync_twice(tmp_path, "confluence", ConfluenceConnector, expected=1)


class TestConfluenceConformance(ConnectorConformance):
    @pytest.fixture(autouse=True)
    def _wire(self, fake_confluence) -> None:
        pass

    def make_connector(self, tmp_path: Path, state: StateStore) -> ConfluenceConnector:
        return ConfluenceConnector(_confluence_source(), state)


# ---------------------------------------------------------------------------
# IMAP (31.6)
# ---------------------------------------------------------------------------

RAW_MESSAGES = {
    "1": (
        b"Message-ID: <m1@example.com>\r\nSubject: Q3 budget approved\r\n"
        b"From: cfo@example.com\r\nTo: team@example.com\r\n"
        b"Date: Thu, 10 Jul 2026 09:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\nThe Q3 budget is approved. Spend wisely.\r\n"
    ),
    "2": (
        b"Message-ID: <m2@example.com>\r\nSubject: Offsite logistics\r\n"
        b"From: ops@example.com\r\nTo: team@example.com\r\nCc: exec@example.com\r\n"
        b"Date: Fri, 11 Jul 2026 10:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\nOffsite is in Lisbon, book flights by Monday.\r\n"
    ),
}


class FakeIMAP:
    def __init__(self) -> None:
        self.messages = dict(RAW_MESSAGES)

    def login(self, user: str, password: str) -> None:
        assert (user, password) == ("bot", "hunter2")

    def select(self, mailbox: str, readonly: bool = False) -> None:
        assert readonly

    def logout(self) -> None:
        pass

    def uid(self, command: str, *args: Any):
        if command == "SEARCH":
            uids = " ".join(sorted(self.messages, key=int)).encode()
            return "OK", [uids]
        if command == "FETCH":
            uid, parts = args
            raw = self.messages[str(uid)]
            if "HEADER.FIELDS" in parts:
                header = raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                return "OK", [(b"1 (BODY[HEADER] {...}", header)]
            return "OK", [(b"1 (BODY[] {...}", raw)]
        raise AssertionError(f"unexpected IMAP command {command}")


@pytest.fixture()
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> FakeIMAP:
    fake = FakeIMAP()
    monkeypatch.setattr("pheasant.connectors.imap._imap_connect", lambda h, p, t: fake)
    monkeypatch.setenv("IMAP_CREDENTIALS", "bot:hunter2")
    return fake


def _imap_source() -> SourceConfig:
    source = _source("imap", path=Path("INBOX"))
    source.connector.api_endpoint = "imap.example.test:993"
    return source


def test_imap_lists_and_renders_messages(tmp_path: Path, fake_imap: FakeIMAP) -> None:
    connector = ImapConnector(_imap_source(), _state(tmp_path))
    items = connector.list_items()
    assert [i.metadata["uid"] for i in items] == ["1", "2"]
    assert items[1].metadata["acl"]["cc"] == "exec@example.com"
    payload = connector.read_item(items[0])
    text = payload.content.decode("utf-8")
    assert "# Q3 budget approved" in text
    assert "From: cfo@example.com" in text
    assert "The Q3 budget is approved." in text
    assert connector.read_item(items[0]).content == payload.content


def test_imap_incremental_lists_only_new_uids(tmp_path: Path, fake_imap: FakeIMAP) -> None:
    connector = ImapConnector(_imap_source(), _state(tmp_path))
    items = connector.list_items()
    cursor, watermark = connector.checkpoint_from_items(items)
    assert cursor == {"last_uid": 2}
    connector.set_checkpoint(cursor, watermark, "healthy")

    connector.begin_sync("incremental")
    assert connector.list_items() == []  # nothing new

    fake_imap.messages["3"] = (
        b"Message-ID: <m3@example.com>\r\nSubject: New starter\r\n"
        b"From: hr@example.com\r\nTo: team@example.com\r\n"
        b"Date: Sat, 12 Jul 2026 08:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\nPlease welcome Ada to the team.\r\n"
    )
    fresh = connector.list_items()
    assert [i.metadata["uid"] for i in fresh] == ["3"]
    cursor2, _ = connector.checkpoint_from_items(fresh)
    assert cursor2 == {"last_uid": 3}  # watermark never regresses


def test_imap_engine_e2e(tmp_path: Path, fake_imap: FakeIMAP) -> None:
    _engine_sync_twice(
        tmp_path,
        "imap",
        ImapConnector,
        expected=2,
        endpoint="imap.example.test:993",
        source_path="INBOX",
        # Messages are immutable: the UID cursor means the second sync lists
        # NOTHING — stronger than a skip (zero fetches, zero comparisons).
        expected_second_skip=0,
    )


class TestImapConformance(ConnectorConformance):
    @pytest.fixture(autouse=True)
    def _wire(self, fake_imap: FakeIMAP) -> None:
        pass

    def make_connector(self, tmp_path: Path, state: StateStore) -> ImapConnector:
        return ImapConnector(_imap_source(), state)
