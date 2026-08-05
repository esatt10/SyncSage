from __future__ import annotations

from pathlib import Path

from pheasant.config.schema import PheasantConfig
from pheasant.obsidian.exporter import ObsidianExporter
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore


def export_obsidian_notes(config: PheasantConfig, vault_path: Path | None = None) -> dict:
    if vault_path is not None:
        config.pheasant.vault_path = vault_path
    paths = StatePaths.from_config(config)
    state = StateStore(paths.sqlite)
    state.migrate()
    return ObsidianExporter(config, state).export()

__all__ = ["ObsidianExporter", "export_obsidian_notes"]
