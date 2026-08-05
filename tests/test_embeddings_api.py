"""Semantic search, configurable from the UI (not just YAML).

Acceptance:

1. ``GET /search/embeddings`` reports enough to render a settings page:
   provider, model, key presence, vector count and coverage.
2. ``PUT`` flips embeddings on **in the live process** — an engine that
   agreed to embed and then didn't would report a successful reindex having
   written nothing.
3. ``POST .../reindex`` embeds already-indexed content, and is idempotent:
   a second run makes zero embedder calls.
4. Changing model/dimensions is flagged as invalidating, and can drop the
   now-meaningless vectors.
5. Persisting writes only the embeddings keys — the rest of the config
   file, `sources` included, survives byte-for-byte in meaning.
6. Enabling embeddings makes `mode="vector"` actually return hits.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.loader import load_config


def _app(config, workspace: Path, config_path: Path | None = None):
    config.pheasant.workspace_root = workspace
    app = create_app(config=config, config_path=config_path)
    app.state.engine.sync_source("architecture-notes", "full")
    return app


def test_status_reports_a_disabled_default(loaded_config, workspace_copy: Path) -> None:
    client = TestClient(_app(loaded_config, workspace_copy))

    status = client.get("/search/embeddings").json()

    assert status["enabled"] is False
    assert status["active"] is False
    assert status["vector_count"] == 0
    assert status["chunk_count"] > 0
    assert status["coverage"] == 0.0
    assert {p["id"] for p in status["providers"]} == {"stub", "openai-spec"}


def test_enabling_takes_effect_in_the_live_process(loaded_config, workspace_copy: Path) -> None:
    app = _app(loaded_config, workspace_copy)
    client = TestClient(app)
    assert app.state.engine.vectors is None

    body = client.put(
        "/search/embeddings",
        json={"enabled": True, "provider": "stub", "store_provider": "numpy", "persist": False},
    ).json()

    assert body["enabled"] is True
    assert body["active"] is True, "the engine must now hold a live vector indexer"
    assert app.state.engine.vectors is not None


def test_reindex_embeds_existing_content_and_is_idempotent(
    loaded_config, workspace_copy: Path
) -> None:
    app = _app(loaded_config, workspace_copy)
    client = TestClient(app)
    client.put(
        "/search/embeddings",
        json={"enabled": True, "provider": "stub", "store_provider": "numpy", "persist": False},
    )

    first = client.post("/search/embeddings/reindex").json()
    assert first["embedded_chunks"] > 0
    assert first["vector_count"] == first["embedded_chunks"]
    assert first["coverage"] > 0

    # Chunk ids are content-addressed, so a second pass has nothing to do.
    second = client.post("/search/embeddings/reindex").json()
    assert second["embedded_chunks"] == 0
    assert second["vector_count"] == first["vector_count"]


def test_vector_mode_returns_hits_once_built(loaded_config, workspace_copy: Path) -> None:
    client = TestClient(_app(loaded_config, workspace_copy))
    empty = client.post("/search", json={"query": "sync engine", "mode": "vector"}).json()
    assert empty["counts"]["vector"] == 0

    client.put(
        "/search/embeddings",
        json={"enabled": True, "provider": "stub", "store_provider": "numpy", "persist": False},
    )
    client.post("/search/embeddings/reindex")

    filled = client.post("/search", json={"query": "sync engine", "mode": "vector"}).json()
    assert filled["counts"]["vector"] > 0
    assert filled["results"]


def test_changing_the_model_flags_and_can_drop_stale_vectors(
    loaded_config, workspace_copy: Path
) -> None:
    client = TestClient(_app(loaded_config, workspace_copy))
    client.put(
        "/search/embeddings",
        json={"enabled": True, "provider": "stub", "store_provider": "numpy", "persist": False},
    )
    client.post("/search/embeddings/reindex")

    # A different dimension means every stored vector is in the wrong space.
    changed = client.put("/search/embeddings", json={"dimensions": 128, "persist": False}).json()
    assert changed["vectors_invalidated"] is True

    dropped = client.post("/search/embeddings/reindex", params={"drop_existing": True}).json()
    assert dropped["embedded_chunks"] > 0
    assert dropped["dimensions_on_disk"] == 128


def test_reindex_without_embeddings_enabled_is_a_4xx(loaded_config, workspace_copy: Path) -> None:
    client = TestClient(_app(loaded_config, workspace_copy))
    response = client.post("/search/embeddings/reindex")
    assert response.status_code == 400
    assert "not enabled" in response.json()["detail"]


def test_persisting_preserves_the_rest_of_the_config_file(
    loaded_config, workspace_copy: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        "  name: keepme\n"
        "search:\n"
        "  default_mode: hybrid\n"
        "obsidian:\n"
        "  note_root: MyNotes\n"
        "sources:\n"
        "  - name: notes\n"
        "    type: markdown_folder\n"
        f"    path: {workspace_copy}\n"
        "    include:\n"
        '      - "**/*.md"\n',
        encoding="utf-8",
    )
    client = TestClient(_app(loaded_config, workspace_copy, config_path=config_path))

    body = client.put(
        "/search/embeddings",
        json={
            "enabled": True,
            "provider": "stub",
            "model": "my-embed",
            "store_provider": "numpy",
            "persist": True,
        },
    ).json()
    assert body["wrote_config"] is True

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["search"]["embeddings"]["enabled"] is True
    assert raw["search"]["embeddings"]["model"] == "my-embed"
    # Everything else survives.
    assert raw["pheasant"]["name"] == "keepme"
    assert raw["obsidian"]["note_root"] == "MyNotes"
    assert raw["search"]["default_mode"] == "hybrid"
    assert [s["name"] for s in raw["sources"]] == ["notes"]
    assert raw["sources"][0]["include"] == ["**/*.md"]

    # And the written file is still loadable.
    reloaded = load_config(config_path)
    assert reloaded.search.embeddings.enabled is True
    assert reloaded.search.embeddings.model == "my-embed"


def test_a_bad_provider_is_reported_not_silently_disabled(
    loaded_config, workspace_copy: Path
) -> None:
    """An unusable provider must not look like a successful enable."""
    client = TestClient(_app(loaded_config, workspace_copy))

    body = client.put(
        "/search/embeddings",
        json={
            "enabled": True,
            "provider": "not-a-real-provider",
            "store_provider": "numpy",
            "persist": False,
        },
    ).json()

    assert body["enabled"] is True
    assert body["active"] is False, "config says on, nothing is actually embedding"


def test_an_uninstalled_vector_store_is_refused_up_front(
    loaded_config, workspace_copy: Path, monkeypatch
) -> None:
    """A backend whose optional extra is missing must fail at enable time.

    Vector backends import lazily, so the indexer for a missing extra builds
    happily and only explodes on first use. Without an up-front check the
    caller is told embeddings are on, and then the reindex 500s.
    """
    app = _app(loaded_config, workspace_copy)
    client = TestClient(app)
    monkeypatch.setattr(
        "pheasant.search.vector_store.vector_store_available",
        lambda provider: provider == "numpy",
    )

    response = client.put(
        "/search/embeddings",
        json={"enabled": True, "provider": "stub", "store_provider": "lancedb"},
    )

    assert response.status_code == 400
    assert "lancedb" in response.json()["detail"]
    assert "numpy" in response.json()["detail"], "the error must name a working alternative"
    # Refused before anything was applied: the process is untouched.
    assert app.state.engine.vectors is None
    assert loaded_config.search.embeddings.enabled is False


def test_a_store_that_fails_on_first_touch_reports_inactive_not_500(
    loaded_config, workspace_copy: Path, monkeypatch
) -> None:
    """`active: true` must mean the store actually works.

    The lazy-import failure mode: availability says yes, the store still
    raises when touched. Enabling has to surface that as a 4xx with the
    install hint, and status must not claim semantic search is live.
    """
    app = _app(loaded_config, workspace_copy)
    client = TestClient(app)

    broken = ModuleNotFoundError(
        "search.vector_store.provider='lancedb' requires the optional extra: "
        "pip install 'pheasant-kb[vector]'"
    )

    def explode(self) -> int:
        raise broken

    monkeypatch.setattr("pheasant.search.vector_store.NumpyVectorStore.count", explode)

    response = client.put(
        "/search/embeddings",
        json={"enabled": True, "provider": "stub", "store_provider": "numpy"},
    )
    assert response.status_code == 400
    assert "pheasant-kb[vector]" in response.json()["detail"]

    status = client.get("/search/embeddings").json()
    assert status["active"] is False, "a store that cannot be touched is not active"
    assert status["store_error"] is not None

    # And the reindex the UI would offer next is a 4xx, never a server error.
    reindex = client.post("/search/embeddings/reindex")
    assert reindex.status_code == 400


def test_status_marks_which_vector_stores_are_installed(
    loaded_config, workspace_copy: Path
) -> None:
    """The picker can only offer backends this deployment can actually run."""
    client = TestClient(_app(loaded_config, workspace_copy))

    stores = client.get("/search/embeddings").json()["store_providers"]

    by_id = {entry["id"]: entry for entry in stores}
    assert by_id["numpy"]["available"] is True, "numpy needs no optional extra"
    assert set(by_id) == {"numpy", "lancedb"}
    assert by_id["lancedb"]["hint"] and "pheasant-kb[vector]" in by_id["lancedb"]["hint"]
