"""Acceptance tests for multi-modal image ingestion (Synapse 25.4 session A).

Fully offline: the deterministic ``StubCaptioner`` turns image bytes into
indexable text (an authored ``<image>.caption.txt`` sidecar wins), which then
flows through the normal chunk -> embed -> graph path. No network, no image
decoder, no model.

Session A is **image only** — audio is session B and is intentionally absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from syncsage.config.schema import SyncSageConfig
from syncsage.ingestion.captioner import StubCaptioner, build_captioner
from syncsage.search.hybrid import HybridSearch
from syncsage.search.sqlite_store import SearchStore
from syncsage.sync.engine import SyncEngine

FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "sample_workspace" / "images" / "diagram.png"
FIXTURE_SIDECAR = FIXTURE_IMAGE.with_name(FIXTURE_IMAGE.name + ".caption.txt")


def _make_image_engine(tmp_path: Path, *, with_embeddings: bool = False) -> SyncEngine:
    images = tmp_path / "workspace" / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "diagram.png").write_bytes(FIXTURE_IMAGE.read_bytes())
    (images / "diagram.png.caption.txt").write_text(
        FIXTURE_SIDECAR.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # A second image with no sidecar exercises the deterministic template path.
    (images / "untitled_photo.png").write_bytes(FIXTURE_IMAGE.read_bytes() + b"\x00")

    search: dict[str, Any] = {}
    if with_embeddings:
        search = {
            "embeddings": {"enabled": True, "provider": "stub", "dimensions": 64},
            "vector_store": {"provider": "numpy"},
        }
    config = SyncSageConfig.model_validate(
        {
            "syncsage": {
                "name": "image-acceptance",
                "state_path": str(tmp_path / "state"),
                "vault_path": str(tmp_path / "vault"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "search": search,
            "sources": [
                {
                    "name": "images",
                    "type": "document_folder",
                    "path": str(images),
                    "include": ["**/*.png", "**/*.md"],
                }
            ],
        }
    )
    return SyncEngine(config)


def _search(engine: SyncEngine, query: str, mode: str = "text") -> list[str]:
    searcher = HybridSearch(SearchStore(engine.state), vector=None)
    payload = searcher.search_context(
        engine.config.knowledge_base_id, query, mode=mode, max_results=5
    )
    return [str(item.get("relative_path") or "") for item in payload["results"]]


# ---------------------------------------------------------------------------
# Captioner unit behavior
# ---------------------------------------------------------------------------


def test_stub_captioner_is_deterministic_and_sidecar_wins() -> None:
    cap = StubCaptioner()
    a = cap.caption(b"image-bytes", "diagram.png")
    b = cap.caption(b"image-bytes", "diagram.png")
    assert a == b  # deterministic per bytes
    assert cap.caption(b"other-bytes", "diagram.png") != a  # bytes discriminate

    authored = cap.caption(b"image-bytes", "diagram.png", sidecar=b"A red square.")
    assert authored == "A red square."


def test_build_captioner_provider_selection() -> None:
    from syncsage.config.schema import CaptionerSettings

    assert isinstance(build_captioner(CaptionerSettings(provider="stub")), StubCaptioner)
    with pytest.raises(ValueError, match="Unsupported"):
        build_captioner(CaptionerSettings(provider="bogus"))


# ---------------------------------------------------------------------------
# Region ingest + self-search (acceptance)
# ---------------------------------------------------------------------------


def test_image_caption_is_searchable(tmp_path: Path) -> None:
    engine = _make_image_engine(tmp_path)
    engine.sync_source("images", mode="full")

    # The artifact is typed "image".
    rows = engine.state.rows("SELECT relative_path, type FROM artifacts ORDER BY relative_path")
    by_path = {row["relative_path"]: row["type"] for row in rows}
    assert by_path.get("diagram.png") == "image"

    # The authored caption is searchable via the region's own hybrid search.
    hits = _search(engine, "architecture diagram router regions")
    assert any("diagram.png" in path for path in hits), hits


def test_resync_unchanged_image_performs_zero_recaption(tmp_path: Path) -> None:
    engine = _make_image_engine(tmp_path)
    engine.sync_source("images", mode="full")
    assert isinstance(engine.captioner, StubCaptioner)
    baseline = engine.captioner.calls
    assert baseline >= 2  # two images captioned on the full sync

    # An incremental re-sync of unchanged images captions nothing: the engine's
    # pre-read sha256 skip never reaches the captioner (zero-work, like 21.4's
    # zero re-embed).
    engine.sync_source("images", mode="incremental")
    assert engine.captioner.calls == baseline


def test_text_only_region_builds_no_captioner(tmp_path: Path) -> None:
    notes = tmp_path / "workspace" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# Notes\n\nplain text only.\n", encoding="utf-8")
    config = SyncSageConfig.model_validate(
        {
            "syncsage": {
                "name": "text-only",
                "state_path": str(tmp_path / "state"),
                "vault_path": str(tmp_path / "vault"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    engine = SyncEngine(config)
    assert engine.captioner is None  # no image globs -> no captioner, no network risk


# ---------------------------------------------------------------------------
# Contract modalities (router will filter on this)
# ---------------------------------------------------------------------------


def test_contract_modalities_include_image_when_image_source_present(tmp_path: Path) -> None:
    engine = _make_image_engine(tmp_path)
    engine.sync_source("images", mode="full")
    from syncsage.synapse.publisher import ContractPublisher

    publisher = ContractPublisher(engine.config, engine.state)
    contract = publisher.build(generated_at="2026-06-21T00:00:00Z")
    assert "image" in contract["capabilities"]["modalities"]
    assert "text" in contract["capabilities"]["modalities"]


def test_contract_modalities_omit_image_for_text_region(tmp_path: Path) -> None:
    notes = tmp_path / "workspace" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# Notes\n\nplain text only.\n", encoding="utf-8")
    config = SyncSageConfig.model_validate(
        {
            "syncsage": {
                "name": "text-only",
                "state_path": str(tmp_path / "state"),
                "vault_path": str(tmp_path / "vault"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    engine = SyncEngine(config)
    engine.sync_source("notes", mode="full")
    from syncsage.synapse.publisher import ContractPublisher

    contract = ContractPublisher(config, engine.state).build(generated_at="2026-06-21T00:00:00Z")
    assert "image" not in contract["capabilities"]["modalities"]
