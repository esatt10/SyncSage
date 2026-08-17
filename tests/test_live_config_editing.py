"""Changing the live knowledge base from the UI.

Acceptance:

1. One section can be validated, persisted and hot-applied on its own — the
   whole-file `PUT /config` always demanded a restart.
2. The reply is **honest**: a section the running process cannot pick up
   reports ``restart_required``, not "saved".
3. Validation runs over the whole config, not the fragment, because sections
   are not independent.
4. Renaming the knowledge base reports the re-index it implies instead of
   silently orphaning the indexed graph.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from pheasant.api.app import LIVE_APPLICABLE_SECTIONS, create_app


def test_sections_are_listed_with_whether_they_apply_live(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    body = client.get("/config/sections").json()
    by_id = {section["id"]: section for section in body["sections"]}

    assert by_id["search"]["live_applicable"] is True
    # The server's bind address cannot change under a running server.
    assert by_id["server"]["live_applicable"] is False
    assert by_id["search"]["values"]["default_mode"] == loaded_config.search.default_mode


def test_every_declared_section_is_a_real_config_field(loaded_config) -> None:
    """A typo in the table would 404 a section that exists, forever."""
    fields = set(loaded_config.model_dump(mode="json"))
    for section in LIVE_APPLICABLE_SECTIONS:
        assert section in fields, section


def test_a_live_section_applies_to_the_running_process(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    response = client.patch(
        "/config/section/graph",
        json={"values": {"memory_entity_bridging": False}, "persist": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["restart_required"] is False
    assert loaded_config.graph.memory_entity_bridging is False


def test_a_non_live_section_is_written_but_reported_as_needing_a_restart(
    loaded_config, config_path: Path
) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    original_port = loaded_config.server.port

    body = client.patch(
        "/config/section/server", json={"values": {"port": 9321}, "persist": True}
    ).json()

    assert body["applied"] is False
    assert body["restart_required"] is True
    assert body["wrote_config"] is True
    # Saying "applied" here would be a lie: the socket is already bound.
    assert loaded_config.server.port == original_port
    written = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    assert written["server"]["port"] == 9321


def test_a_patch_merges_rather_than_replaces_the_section(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    before = loaded_config.search.max_results_default

    client.patch(
        "/config/section/search", json={"values": {"default_mode": "text"}, "persist": False}
    )

    assert loaded_config.search.default_mode == "text"
    assert loaded_config.search.max_results_default == before


def test_an_invalid_value_is_rejected_and_changes_nothing(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    before = loaded_config.server.port

    response = client.patch(
        "/config/section/server", json={"values": {"port": "not-a-port"}, "persist": True}
    )

    assert response.status_code == 400
    assert loaded_config.server.port == before
    # A rejected patch must not have written a half-applied file either.
    written = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    assert written.get("server", {}).get("port") != "not-a-port"


def test_an_unknown_section_is_a_404_that_names_the_real_ones(
    loaded_config, config_path: Path
) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    response = client.patch("/config/section/nope", json={"values": {}})
    assert response.status_code == 404
    assert "search" in response.json()["detail"]


def test_values_must_be_a_mapping(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    response = client.patch("/config/section/search", json={"values": ["nope"]})
    assert response.status_code == 422 or response.status_code == 400


# ------------------------------------------------------- knowledge base


def test_knowledge_base_identity_is_readable(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    body = client.get("/knowledge-base").json()
    assert body["name"] == loaded_config.pheasant.name
    assert body["config_path"] == str(config_path)


def test_the_description_changes_freely(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    body = client.put("/knowledge-base", json={"description": "Team notes"}).json()

    assert body["changed"] == ["description"]
    assert body["restart_required"] is False
    assert body["rename"] is None
    assert loaded_config.pheasant.description == "Team notes"


def test_renaming_reports_the_reindex_it_implies(loaded_config, config_path: Path) -> None:
    """The name IS the kb_id: the graph root node and every stable artifact
    id hang off it. Accepting a rename silently leaves the user with an
    apparently-empty knowledge base and no explanation."""
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    previous = loaded_config.pheasant.name

    body = client.put("/knowledge-base", json={"name": "renamed-kb"}).json()

    assert body["restart_required"] is True
    assert body["rename"]["previous"] == previous
    assert body["rename"]["current"] == "renamed-kb"
    assert body["rename"]["reindex_required"] is True
    assert "full" in body["rename"]["detail"]
    # The live process keeps the old id — repointing it mid-flight would write
    # into a graph this process has not loaded.
    assert loaded_config.pheasant.name == previous
    written = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    assert written["pheasant"]["name"] == "renamed-kb"


def test_an_empty_name_is_rejected(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    assert client.put("/knowledge-base", json={"name": "   "}).status_code == 400


def test_no_changes_is_reported_as_unchanged(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    body = client.put("/knowledge-base", json={"name": loaded_config.pheasant.name}).json()
    assert body["status"] == "unchanged"
