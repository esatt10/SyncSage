from __future__ import annotations

from pathlib import Path

from syncsage.config.schema import SyncSageConfig
from syncsage.obsidian.exporter import ObsidianExporter
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore


def export_obsidian_notes(config: SyncSageConfig, vault_path: Path | None = None) -> dict:
    if vault_path is not None:
        config.syncsage.vault_path = vault_path
    paths = StatePaths.from_config(config)
    state = StateStore(paths.sqlite)
    state.migrate()
    return ObsidianExporter(config, state).export()

__all__ = ["ObsidianExporter", "export_obsidian_notes"]
