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
    source = memory_source(config, getattr(engine, "state", None))
    if source is None:
        return None
    store = MemoryStore(source.path)
    report = store.consolidate(
        now=now,
        session_ttl_days=settings.session_ttl_days,
        user_ttl_days=settings.user_ttl_days,
        org_ttl_days=settings.org_ttl_days,
    )
    result: dict[str, Any] = {"source": source.name, "report": report.as_dict()}
    pruned = _prune_to_capacity(engine, store, settings, now=now)
    if pruned:
        result["pruned"] = pruned
    if report.archived or pruned:
        result["sync"] = engine.sync_source(source.name, "full").__dict__
    return result


def _prune_to_capacity(engine: Any, store: MemoryStore, settings: Any, *, now=None) -> list[str]:
    """Archive the least salient records once the store exceeds its cap.

    Step 33.9. The only mechanism here that decides what to *forget* on
    grounds other than a correction or an explicit TTL, so it is deliberately
    the most conservative: `memory.max_records` defaults to `None` (unbounded,
    the pre-33.9 behavior), the ranking is a documented deterministic formula,
    and archiving is the same in-place rename consolidation already uses —
    bytes preserved, nothing deleted.

    Salience is also written back so an operator can see the score that
    decided it rather than having to recompute the formula by hand.
    """
    from pheasant.memory.policy import STEERING_KINDS
    from pheasant.memory.salience import over_capacity, salience

    max_records = getattr(settings, "max_records", None)
    rows = engine.state.memory_salience_rows()
    if not rows:
        return []

    engine.state.set_memory_salience({str(r["record_id"]): salience(r, now=now) for r in rows})
    if not max_records:
        return []

    # `alias`/`preference`/`exclusion` records are retrieval *machinery*, not
    # recallable content, and they are exempt from the cap in both directions:
    # they neither consume slots nor get archived. Ranking them by salience put
    # a deliberate operator-written rule in competition with ordinary facts on
    # a formula built for facts — recency decay and use counts — so crossing
    # `max_records` could silently switch off an `exclusion` with no signal
    # anywhere, changing ranking for every future query. The number of rules
    # actually in force is already bounded, by `steering.MAX_RULES`.
    prunable = [row for row in rows if str(row.get("kind") or "") not in STEERING_KINDS]
    if not prunable:
        return []

    doomed = {str(row["record_id"]) for row in over_capacity(prunable, max_records, now=now)}
    if not doomed:
        return []
    archived: list[str] = []
    for record in store.list_records():
        if record.record_id in doomed:
            store.archive(record)
            archived.append(record.record_id)
    return sorted(archived)
