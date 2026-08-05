"""Memory consolidation maintenance (Product Framework Step 33.2).

One pass = ``MemoryStore.consolidate`` (archive superseded / TTL-expired
record files — a pure content operation) followed by a **full** sync of the
memory source when anything was archived, so the ordinary pipeline drops the
archived records from the index (incremental mode never prunes; full mode
rebuilds the small memory source deterministically). Runs from the 21.1
scheduler loop, and on demand via the ``memory_consolidate`` MCP tool /
``POST /memory/consolidate``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pheasant.memory.store import MemoryStore, memory_source


def run_memory_maintenance(engine: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Consolidate the configured memory source; None when not applicable.

    Callers own sync serialization — run this under the same lock as other
    engine syncs (the scheduler does).
    """
    config = engine.config
    settings = getattr(config, "memory", None)
    if settings is None or not settings.consolidation_enabled:
        return None
    source = memory_source(config)
    if source is None:
        return None
    report = MemoryStore(source.path).consolidate(
        now=now,
        session_ttl_days=settings.session_ttl_days,
        user_ttl_days=settings.user_ttl_days,
        org_ttl_days=settings.org_ttl_days,
    )
    result: dict[str, Any] = {"source": source.name, "report": report.as_dict()}
    if report.archived:
        result["sync"] = engine.sync_source(source.name, "full").__dict__
    return result
