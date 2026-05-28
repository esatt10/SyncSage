"""Acceptance tests for idempotent Obsidian Markdown export."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import first_attr, import_any, require_attr, run_sync


def _export_notes(loaded_config: object, sync_engine: object, vault_path: Path) -> object:
    if first_attr(sync_engine, ("export_obsidian_notes", "export_obsidian", "export_notes")):
        exporter = require_attr(
            sync_engine,
            ("export_obsidian_notes", "export_obsidian", "export_notes"),
            "Obsidian export method",
        )
        try:
            return exporter(vault_path=vault_path)
        except TypeError:
            return exporter()

    export_module = import_any(
        ("syncsage.obsidian", "syncsage.export.obsidian", "syncsage.api.tools")
    )
    exporter = require_attr(
        export_module,
        ("export_obsidian_notes", "export_obsidian", "export_notes"),
        "Obsidian exporter",
    )
    try:
        return exporter(config=loaded_config, vault_path=vault_path)
    except TypeError:
        return exporter(loaded_config)


def test_obsidian_export_updates_notes_in_place(
    loaded_config: object,
    sync_engine: object,
    vault_path: Path,
) -> None:
    """Repeated exports should update Markdown notes without duplicating them."""

    run_sync(sync_engine, source_name="syncsage-repo", mode="full")

    _export_notes(loaded_config, sync_engine, vault_path)
    first_notes = sorted(
        path.relative_to(vault_path).as_posix() for path in vault_path.rglob("*.md")
    )
    assert first_notes, "Expected Obsidian export to create Markdown notes."
    first_contents = {path: (vault_path / path).read_text(encoding="utf-8") for path in first_notes}

    _export_notes(loaded_config, sync_engine, vault_path)
    second_notes = sorted(
        path.relative_to(vault_path).as_posix() for path in vault_path.rglob("*.md")
    )
    second_contents = {
        path: (vault_path / path).read_text(encoding="utf-8") for path in second_notes
    }

    assert second_notes == first_notes
    assert second_contents == first_contents
    assert any(
        "---" in content and "source" in content.lower()
        for content in second_contents.values()
    )


def test_obsidian_preview_and_graph_driven_navigation(
    loaded_config: object,
    sync_engine: object,
    vault_path: Path,
) -> None:
    """Preview should not write files; export should create graph-navigation links."""

    loaded_config.obsidian.create_chunk_notes = True
    run_sync(sync_engine, source_name="syncsage-repo", mode="full")

    preview = sync_engine.export_obsidian_notes(
        vault_path=vault_path,
        preview=True,
        template_profile="research",
    )

    assert preview["preview"] is True
    assert preview["planned_count"] > 0
    assert preview["changed_count"] == preview["planned_count"]
    assert not list(vault_path.rglob("*.md"))

    result = sync_engine.export_obsidian_notes(
        vault_path=vault_path,
        template_profile="research",
    )

    root = vault_path / loaded_config.obsidian.note_root
    source_note = root / "Sources" / "syncsage-repo.md"
    file_note = root / "Files" / "README.md.md"
    chunk_notes = sorted((root / "Chunks").glob("*.md"))

    assert result["count"] == result["planned_count"]
    assert source_note.exists()
    assert file_note.exists()
    assert chunk_notes
    assert "../Concepts/" in source_note.read_text(encoding="utf-8")
    assert "../Chunks/" in file_note.read_text(encoding="utf-8")
    assert "../Files/" in chunk_notes[0].read_text(encoding="utf-8")
