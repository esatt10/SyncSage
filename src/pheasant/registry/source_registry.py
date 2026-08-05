from __future__ import annotations

from datetime import UTC, datetime

from pheasant.config.loader import config_hash
from pheasant.config.schema import PheasantConfig, SourceConfig
from pheasant.persistence.state_store import StateStore


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SourceRegistry:
    def __init__(self, config: PheasantConfig, state: StateStore):
        self.config = config
        self.state = state

    def initialize(self) -> None:
        self.state.upsert_knowledge_base(
            self.config.knowledge_base_id,
            self.config.pheasant.name,
            self.config.pheasant.description,
            config_hash(self.config),
            now(),
        )
        for source in self.config.sources:
            self.register_source(source)

    def register_source(self, source: SourceConfig) -> None:
        self.state.upsert_source(
            source.name,
            self.config.knowledge_base_id,
            source.name,
            source.type.value,
            str(source.path),
            source.enabled,
            source.model_dump(mode="json"),
        )

    def list_sources(
        self,
        enabled: bool | None = None,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        checkpoints = {
            checkpoint["source_id"]: checkpoint
            for checkpoint in self.state.list_source_checkpoints()
        }
        where = []
        params: list[object] = []
        if enabled is not None:
            where.append("enabled=?")
            params.append(int(enabled))
        if status:
            where.append("last_status=?")
            params.append(status)
        if source_type:
            where.append("type=?")
            params.append(source_type)
        clause = "WHERE " + " AND ".join(where) if where else ""
        params.extend([limit, offset])
        sources = []
        for row in self.state.rows(
            f"SELECT * FROM sources {clause} ORDER BY name LIMIT ? OFFSET ?",
            tuple(params),
        ):
            source = dict(row)
            source["checkpoint"] = checkpoints.get(source["id"])
            sources.append(source)
        return sources
