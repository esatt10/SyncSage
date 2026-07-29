"""Product Steps 32.1/32.2/32.6 — ACL persistence, principal filtering, leak gate.

The adversarial contract: with enforcement on, a caller can NEVER retrieve
an artifact their identity set does not admit — across text and hybrid
modes, the MCP facade, and HTTP /search. With enforcement off (default),
behavior is identical to pre-32.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from syncsage.config.loader import load_config
from syncsage.mcp_server.tools import SyncSageTools
from syncsage.security.acl import expand_principal, is_allowed, normalize_acl
from syncsage.sync import connector_registry
from syncsage.sync.connectors import ConnectorItem, ConnectorPayload, SourceConnector

DOCS = {
    "alice-notes.txt": ("Project mercury launch codes for alice.", {"allow": ["user:alice"]}),
    "bob-notes.txt": ("Project gemini payroll data for bob.", {"allow": ["user:bob"]}),
    "eng-runbook.txt": ("Project apollo runbook for engineering.", {"allow": ["group:eng"]}),
    "handbook.txt": ("Company handbook, public to all.", {"public": True}),
}


class AclTestConnector(SourceConnector):
    connector_type = "acltest"

    def list_items(self) -> list[ConnectorItem]:
        import hashlib

        items = []
        for name, (text, acl) in sorted(DOCS.items()):
            items.append(
                ConnectorItem(
                    identity=f"acltest:{self.source.name}:{name}",
                    relative_path=name,
                    uri=f"acltest://{name}",
                    mime_type="text/plain",
                    sha256=hashlib.sha256(text.encode()).hexdigest(),
                    metadata={"acl": dict(acl), "text": text},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        text = str(item.metadata["text"])
        return ConnectorPayload(item=item, content=text.encode(), mime_type="text/plain")


def _config(tmp_path: Path, *, enforced: bool) -> Any:
    config_path = tmp_path / "syncsage.yaml"
    config_path.write_text(
        f"""syncsage:
  name: acl-test
  state_path: {tmp_path / "state"}
  vault_path: {tmp_path / "vault"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
security:
  acl_enforced: {str(enforced).lower()}
  groups:
    carol:
      - eng
sources:
  - name: docs
    type: acltest
    path: /unused
    include: []
""",
        encoding="utf-8",
    )
    return load_config(config_path)


@pytest.fixture()
def synced_tools(tmp_path: Path):
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    tools = SyncSageTools(_config(tmp_path, enforced=True))
    tools.sync_source("acl-test", "docs", "incremental")
    yield tools
    tools.engine.close()
    connector_registry.reset_connector_registry()


def _titles(tools: SyncSageTools, query: str, **kwargs: Any) -> set[str]:
    payload = tools.search_context("acl-test", query, mode="text", max_results=10, **kwargs)
    return {str(r.get("relative_path")) for r in payload["results"]}


def test_leak_gate_principal_scoping(synced_tools: SyncSageTools) -> None:
    # Alice: her doc + public. NEVER bob's.
    seen = _titles(synced_tools, "project handbook", principal="user:alice")
    assert "alice-notes.txt" in seen and "handbook.txt" in seen
    assert "bob-notes.txt" not in seen and "eng-runbook.txt" not in seen

    # Bob: symmetric.
    seen = _titles(synced_tools, "project handbook", principal="user:bob")
    assert "bob-notes.txt" in seen and "alice-notes.txt" not in seen

    # Anonymous: public only.
    seen = _titles(synced_tools, "project handbook")
    assert seen <= {"handbook.txt"}

    # Group via explicit groups param, and via config-mapped groups (carol→eng).
    seen = _titles(
        synced_tools, "project handbook", principal="user:dave", principal_groups=["group:eng"]
    )
    assert "eng-runbook.txt" in seen and "bob-notes.txt" not in seen
    seen = _titles(synced_tools, "project handbook", principal="user:carol")
    assert "eng-runbook.txt" in seen

    # Hybrid mode is equally scoped (graph nodes conservatively denied).
    payload = synced_tools.search_context(
        "acl-test", "project handbook", mode="hybrid", max_results=10, principal="user:alice"
    )
    flat = str(payload).lower()
    assert "gemini payroll" not in flat


def test_enforcement_off_is_pre32_behavior(tmp_path: Path) -> None:
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    tools = SyncSageTools(_config(tmp_path, enforced=False))
    try:
        tools.sync_source("acl-test", "docs", "incremental")
        seen = _titles(tools, "project handbook")
        assert {"alice-notes.txt", "bob-notes.txt", "eng-runbook.txt"} <= seen
    finally:
        tools.engine.close()
        connector_registry.reset_connector_registry()


def test_http_search_forwards_principal(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from syncsage.api.app import create_app

    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    config = _config(tmp_path, enforced=True)
    app = create_app(config, config_path=str(tmp_path / "syncsage.yaml"))
    client = fastapi_testclient.TestClient(app)
    try:
        app.state.engine.sync_source("docs", "incremental")
        body = {"query": "project", "mode": "text", "principal": "user:alice"}
        hits = client.post("/search", json=body).json()["results"]
        paths = {h.get("relative_path") for h in hits}
        assert "alice-notes.txt" in paths and "bob-notes.txt" not in paths
        anon = client.post("/search", json={"query": "project", "mode": "text"}).json()["results"]
        assert {h.get("relative_path") for h in anon} <= {"handbook.txt"}
    finally:
        app.state.engine.close()
        connector_registry.reset_connector_registry()


def test_acl_normalization_rules() -> None:
    assert normalize_acl("notion", {"created_by": "u1", "last_edited_by": "u2"}) == {
        "allow": ["user:u1", "user:u2"],
        "public": False,
    }
    assert normalize_acl("slack", {"is_private": False}) == {"allow": [], "public": True}
    assert normalize_acl("slack", {"is_private": True}) is None
    assert normalize_acl("confluence", {"space": "OPS", "created_by": "a1"}) == {
        "allow": ["group:space:OPS", "user:a1"],
        "public": False,
    }
    imap = normalize_acl("imap", {"from": "CFO <cfo@x.com>", "to": "a@x.com, b@x.com", "cc": ""})
    assert imap == {"allow": ["user:a@x.com", "user:b@x.com", "user:cfo@x.com"], "public": False}
    assert normalize_acl("gdrive", {"owners": ["o@x.com"], "shared": True})["allow"] == [
        "user:o@x.com"
    ]
    assert normalize_acl("anything", None) is None


def test_is_allowed_semantics() -> None:
    ids = expand_principal("user:alice", None, {"user:alice": ["eng"]})
    assert ids == {"user:alice", "group:eng"}
    assert is_allowed('{"allow": ["group:eng"], "public": false}', ids, default_public=True)
    assert not is_allowed('{"allow": ["user:bob"], "public": false}', ids, default_public=True)
    assert is_allowed(None, None, default_public=True)  # un-ACL'd public default
    assert not is_allowed(None, None, default_public=False)  # private default, anonymous
    assert is_allowed(None, ids, default_public=False)  # private default, authenticated
    assert not is_allowed("not-json", ids, default_public=True)  # fails closed
