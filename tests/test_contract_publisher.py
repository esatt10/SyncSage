"""Acceptance tests for the contract publisher + sync event stream (21.5).

Fully offline:

- the stub embedder (planted synonyms) provides chunk vectors so the signature
  has real clusters;
- schema validation uses the vendored ``contracts/semantic_contract.v1.schema.json``;
- the router webhook is exercised against a stdlib ``http.server`` bound to a
  loopback port (no external network).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.synapse.publisher import CONTRACT_FILENAME, ContractPublisher, contract_path
from pheasant.sync.engine import SyncEngine
from tests.conftest import make_vector_engine, run_sync

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (REPO_ROOT / "contracts" / "semantic_contract.v1.schema.json").read_text(encoding="utf-8")
)

PINNED = "2026-06-18T12:00:00Z"


def _publish_engine(tmp_path: Path, *, embeddings: bool = True, **synapse: Any) -> SyncEngine:
    """A SyncEngine over the tiny vector workspace with publishing on."""

    if embeddings:
        engine = make_vector_engine(tmp_path)
    else:
        engine = _no_embed_engine(tmp_path)
    engine.config.synapse.publish = True
    for key, value in synapse.items():
        setattr(engine.config.synapse, key, value)
    # Rebuild the webhook so an injected router_url takes effect.
    from pheasant.synapse.events import RouterWebhook

    engine.router_webhook = RouterWebhook(
        engine.config.synapse.router_url,
        timeout=engine.config.synapse.webhook_timeout_seconds,
    )
    return engine


def _no_embed_engine(tmp_path: Path) -> SyncEngine:
    notes = tmp_path / "ws" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "vehicles.md").write_text(
        "# Fleet\n\nThe automobile fleet needs quarterly inspection.\n", encoding="utf-8"
    )
    (notes / "kitchen.md").write_text(
        "# Kitchen\n\nWash dishes nightly and restock pantry shelves.\n", encoding="utf-8"
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "no-embed-region",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "ws"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    return SyncEngine(config)


# ---------------------------------------------------------------------------
# Contract structure + schema validity
# ---------------------------------------------------------------------------


def test_published_contract_validates_against_vendored_schema(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")

    path = contract_path(engine.config.pheasant.state_path)
    assert path.name == CONTRACT_FILENAME
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))

    jsonschema.validate(payload, SCHEMA)
    assert payload["kind"] == "semantic_contract"
    assert payload["schema_version"] == 1
    assert payload["kb_id"] == "vector-acceptance"
    assert payload["embedding_space"]["dim"] == 64
    assert payload["signature"]["sample_count"] >= 1
    assert len(payload["signature"]["cluster_weights"]) == len(
        payload["signature"]["cluster_centroids"]
    )
    assert payload["capabilities"]["returns_vectors"] is True
    assert "vector" in payload["capabilities"]["search_modes"]
    assert payload["integrity"]["content_hash"].startswith("sha256:")
    assert payload["watermark"]["source_checkpoints_digest"].startswith("sha256:")
    assert payload["vocabulary"]["top_concepts"]


def test_content_hash_seals_the_body(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    payload = json.loads(contract_path(engine.config.pheasant.state_path).read_text("utf-8"))
    from pheasant.synapse.publisher import _content_hash

    body = {k: v for k, v in payload.items() if k != "integrity"}
    assert payload["integrity"]["content_hash"] == _content_hash(body)


def test_no_vector_fallback_emits_degenerate_signature(tmp_path: Path) -> None:
    """Embeddings disabled: a schema-valid contract with a zero signature."""

    engine = _publish_engine(tmp_path, embeddings=False)
    assert engine.vectors is None
    run_sync(engine, source_name="notes", mode="full")

    payload = json.loads(contract_path(engine.config.pheasant.state_path).read_text("utf-8"))
    jsonschema.validate(payload, SCHEMA)
    assert payload["signature"]["sample_count"] == 0
    assert payload["signature"]["cluster_centroids"] == []
    assert payload["capabilities"]["returns_vectors"] is False
    assert "vector" not in payload["capabilities"]["search_modes"]
    # Vocabulary + watermark still carry real content.
    assert payload["vocabulary"]["top_concepts"]
    assert payload["watermark"]["artifact_count"] >= 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_unchanged_content_yields_identical_digest_and_bytes(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    publisher = ContractPublisher(engine.config, engine.state, vector_store=engine.vectors.store)
    first = publisher.build(generated_at=PINNED)

    # A second sync with no content change must leave the watermark digest and
    # (with a pinned generated_at) the whole contract byte-identical.
    run_sync(engine, source_name="notes", mode="incremental")
    second = publisher.build(generated_at=PINNED)

    assert (
        first["watermark"]["source_checkpoints_digest"]
        == second["watermark"]["source_checkpoints_digest"]
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_content_change_moves_the_digest(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    publisher = ContractPublisher(engine.config, engine.state, vector_store=engine.vectors.store)
    before = publisher.build(generated_at=PINNED)["watermark"]["source_checkpoints_digest"]

    notes_dir = Path(engine.config.sources[0].path)
    (notes_dir / "kitchen.md").write_text(
        "# Kitchen\n\nA brand new pantry inventory line.\n", encoding="utf-8"
    )
    run_sync(engine, source_name="notes", mode="incremental")
    after = publisher.build(generated_at=PINNED)["watermark"]["source_checkpoints_digest"]
    assert before != after


# ---------------------------------------------------------------------------
# Router webhook
# ---------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    posts: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).posts.append({"path": self.path, "body": json.loads(body.decode("utf-8"))})
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:  # silence test server
        return


@pytest.fixture()
def router_server():
    _Recorder.posts = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", _Recorder
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_webhook_fires_exactly_once_with_inline_contract(tmp_path: Path, router_server) -> None:
    router_url, recorder = router_server
    engine = _publish_engine(tmp_path, router_url=router_url, fleet_id="fleet-x")
    run_sync(engine, source_name="notes", mode="full")

    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post["path"] == "/v1/synapse/events"
    event = post["body"]
    assert event["type"] == "sync.completed"
    assert event["kb_id"] == "vector-acceptance"
    assert event["fleet_id"] == "fleet-x"
    # The inline contract lets the router upsert without a pull.
    assert event["contract"]["kind"] == "semantic_contract"
    jsonschema.validate(event["contract"], SCHEMA)


def test_router_down_does_not_fail_the_sync(tmp_path: Path) -> None:
    # Point at a closed loopback port; the webhook must swallow the failure.
    engine = _publish_engine(tmp_path, router_url="http://127.0.0.1:9")
    result = run_sync(engine, source_name="notes", mode="full")
    assert result.status == "healthy"
    # Contract still published locally despite the router being unreachable.
    assert contract_path(engine.config.pheasant.state_path).exists()


def test_no_webhook_when_router_unset(tmp_path: Path, router_server) -> None:
    router_url, recorder = router_server
    engine = _publish_engine(tmp_path)  # publish on, router unset
    run_sync(engine, source_name="notes", mode="full")
    assert recorder.posts == []


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------


def test_sync_completed_event_appended_to_ndjson(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    events = engine.events.read_all()
    completed = [e for e in events if e["type"] == "sync.completed"]
    assert len(completed) == 1
    assert completed[0]["source_id"] == "notes"
    assert completed[0]["kb_id"] == "vector-acceptance"
    # Daily file naming.
    files = list((Path(engine.config.pheasant.state_path) / "events").glob("*.ndjson"))
    assert files and files[0].name.endswith(".ndjson")


# ---------------------------------------------------------------------------
# Standalone (publish off) — behavior unchanged
# ---------------------------------------------------------------------------


def test_standalone_no_publish_no_contract_no_events(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)  # synapse.publish defaults to False
    assert engine.config.synapse.publish is False
    run_sync(engine, source_name="notes", mode="full")
    assert not contract_path(engine.config.pheasant.state_path).exists()
    # No sync.completed events emitted to the stream when publishing is off...
    events = engine.events.read_all()
    assert all(e["type"] != "sync.completed" for e in events)


# ---------------------------------------------------------------------------
# Serving: GET /contract + MCP resource
# ---------------------------------------------------------------------------


def test_http_get_contract_404_before_and_200_after(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    engine = _publish_engine(tmp_path)
    app = create_app(config=engine.config)
    client = TestClient(app)

    # Nothing published yet.
    assert client.get("/contract").status_code == 404

    run_sync(engine, source_name="notes", mode="full")

    response = client.get("/contract")
    assert response.status_code == 200
    payload = response.json()
    jsonschema.validate(payload, SCHEMA)
    assert payload["kb_id"] == "vector-acceptance"


def test_mcp_tool_returns_contract(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")

    from pheasant.mcp_server.tools import PheasantTools

    tools = PheasantTools(engine.config)
    payload = tools.get_contract(engine.config.knowledge_base_id)
    jsonschema.validate(payload, SCHEMA)
    assert payload["kb_id"] == "vector-acceptance"


def test_mcp_tool_unpublished_before_sync(tmp_path: Path) -> None:
    engine = _publish_engine(tmp_path)
    from pheasant.mcp_server.tools import PheasantTools

    tools = PheasantTools(engine.config)
    payload = tools.get_contract(engine.config.knowledge_base_id)
    assert payload["status"] == "unpublished"
