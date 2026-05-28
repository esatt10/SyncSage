from __future__ import annotations

from datetime import UTC, datetime

from syncsage.config.loader import config_hash
from syncsage.config.schema import SourceConfig, SyncSageConfig
from syncsage.persistence.state_store import StateStore


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SourceRegistry:
    def __init__(self, config: SyncSageConfig, state: StateStore):
        self.config = config
        self.state = state

    def initialize(self) -> None:
        self.state.upsert_knowledge_base(
            self.config.knowledge_base_id,
            self.config.syncsage.name,
            self.config.syncsage.description,
            config_hash(self.config),
            now(),
        )
        for source in self.config.sources:
            self.register_source(source)

    def register_source(self, source: SourceConfig) -> None:
        self.state.upsert_source(source.name, self.config.knowledge_base_id, source.name, source.type.value, str(source.path), source.enabled, source.model_dump(mode="json"))

    def list_sources(self) -> list[dict]:
        checkpoints = {
            checkpoint["source_id"]: checkpoint
            for checkpoint in self.state.list_source_checkpoints()
        }
        sources = []
        for row in self.state.rows("SELECT * FROM sources ORDER BY name"):
            source = dict(row)
            source["checkpoint"] = checkpoints.get(source["id"])
            sources.append(source)
        return sources
