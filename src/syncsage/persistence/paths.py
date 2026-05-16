from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from syncsage.config.schema import SyncSageConfig


@dataclass(frozen=True)
class StatePaths:
    state: Path
    sqlite: Path
    graphs: Path
    manifests: Path
    snapshots: Path
    locks: Path
    logs: Path
    cache: Path
    vault: Path
    exports: Path

    @classmethod
    def from_config(cls, config: SyncSageConfig) -> "StatePaths":
        state = config.syncsage.state_path
        return cls(
            state=state,
            sqlite=config.storage.sqlite_path or state / "syncsage.db",
            graphs=config.storage.graph_path or state / "graphs",
            manifests=config.storage.manifest_path or state / "manifests",
            snapshots=state / "snapshots",
            locks=state / "locks",
            logs=state / "logs",
            cache=state / "cache",
            vault=config.syncsage.vault_path,
            exports=config.syncsage.exports_path,
        )

    def ensure(self) -> None:
        for path in [self.state, self.graphs, self.manifests, self.snapshots, self.locks, self.logs, self.cache, self.vault, self.exports]:
            path.mkdir(parents=True, exist_ok=True)
        self.sqlite.parent.mkdir(parents=True, exist_ok=True)
