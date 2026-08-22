"""Memory consolidation maintenance (Product Framework Step 33.2).

One pass = ``MemoryStore.consolidate`` (archive superseded / TTL-expired
record files — a pure content operation) followed by a **targeted** removal
of just those records' indexed state when anything was archived (Phase 0):
below ``MEMORY_TARGETED_ARCHIVE_MAX`` archived records, delete their rows and
rebuild the (small) memory projection directly; at or above it, fall back to
the unconditional full sync the pre-Phase-0 code always ran. Runs from the
21.1 scheduler loop, and on demand via the ``memory_consolidate`` MCP tool /
``POST /memory/consolidate``.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from pheasant.memory.store import MemoryRecord, MemoryStore, memory_source
from pheasant.telemetry import metrics

#: Above this many records archived in one pass, fall back to a full sync
#: instead of deleting each artifact's rows individually. `chunks_fts`'s
#: `artifact_id` column is UNINDEXED (CLAUDE.md §6), so each targeted delete
#: degrades to a scan of the whole FTS table — fine for a handful of
#: archived records, the wrong trade once a pass archives most of the store
#: (the measured shape of that trap: 500 -> 8,000 files went 1.9s -> 164.7s
#: under the per-row path, Phase 35.7). A full sync of the memory source
#: rebuilds `chunks_fts` wholesale instead, which is cheap regardless of how
#: many records changed. SQLite-only concern: Postgres has
#: `idx_chunks_fts_artifact` and pays neither cost.
MEMORY_TARGETED_ARCHIVE_MAX = 500


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
    started = time.perf_counter()
    try:
        return _run(engine, config, settings, source, now=now)
    finally:
        metrics.REGISTRY.observe(
            "pheasant_memory_maintenance_seconds", time.perf_counter() - started
        )


def _run(engine: Any, config: Any, settings: Any, source: Any, *, now: datetime | None) -> dict:
    store = MemoryStore(source.path)
    # Phase 0: parse the store once for the whole pass. `consolidate` would
    # otherwise re-glob and re-parse every file itself; handing it the
    # already-loaded list removes that second pass. `by_id` is kept for the
    # artifact-id lookups below — record identity (`record_id` + `scope`)
    # does not change when a file is later archived, only its path does.
    records = store.list_records()
    by_id: dict[str, MemoryRecord] = {r.record_id: r for r in records}
    report = store.consolidate(
        now=now,
        session_ttl_days=settings.session_ttl_days,
        user_ttl_days=settings.user_ttl_days,
        org_ttl_days=settings.org_ttl_days,
        supersede_retention_days=getattr(settings, "supersede_retention_days", 0) or 0,
        records=records,
    )
    result: dict[str, Any] = {"source": source.name, "report": report.as_dict()}
    pruned = _prune_to_capacity(engine, store, settings, now=now)
    if pruned:
        result["pruned"] = pruned
    archived_ids = [*report.archived_superseded, *report.archived_expired, *pruned]
    if archived_ids:
        result["sync"] = _drop_archived(engine, store, source, by_id, archived_ids)

    if getattr(settings, "compaction_enabled", False):
        # Phase 3: cluster and demote near-duplicates. Records archived by
        # this same pass are excluded — they have no live file left, and
        # SQL (which the compaction pass also reads, for observations/tier)
        # will not reflect their removal until the next projection rebuild.
        archived = set(archived_ids)
        live_records = [r for r in records if r.record_id not in archived]
        from pheasant.memory.compaction import run_compaction

        compaction = run_compaction(engine, live_records, settings, now=now)
        if compaction.get("subsumed"):
            result["compaction"] = compaction

    _record_scope_gauge(store)
    return result


def _drop_archived(
    engine: Any,
    store: MemoryStore,
    source: Any,
    by_id: dict[str, MemoryRecord],
    archived_ids: list[str],
) -> dict[str, Any]:
    """Remove indexed state for just-archived records (Phase 0).

    Below `MEMORY_TARGETED_ARCHIVE_MAX`: delete the archived artifacts' rows
    directly — state, graph nodes, vectors — and rebuild the `memory_records`
    projection. That is the exact set of things a full sync of the memory
    source would otherwise redo from scratch (re-list, re-parse and
    re-chunk every *surviving* record too) just to drop a handful that are
    gone. At or above the threshold, defer to the unconditional full sync;
    see `MEMORY_TARGETED_ARCHIVE_MAX`.
    """
    from pheasant.memory.projection import artifact_id_for, project

    if len(archived_ids) >= MEMORY_TARGETED_ARCHIVE_MAX:
        return engine.sync_source(source.name, "full").__dict__

    artifact_ids = [
        artifact_id_for(source.name, by_id[record_id])
        for record_id in archived_ids
        if record_id in by_id
    ]
    if artifact_ids:
        engine.state.delete_artifacts(artifact_ids)
        engine.graph_builder.remove_artifact_nodes(artifact_ids)
        if engine.vectors is not None:
            live_chunk_ids = {
                str(row["id"])
                for row in engine.state.rows(
                    "SELECT id FROM chunks WHERE source_id=?", (source.name,)
                )
            }
            engine.vectors.prune_source(source.name, live_chunk_ids)
    # `store.list_records()` re-globs the now-current directory (archived
    # files no longer match `mem-*.md`), but the parse-once cache means every
    # record untouched by this pass is served from memory rather than
    # re-read from disk.
    engine.state.replace_memory_records(source.name, project(source.name, store.list_records()))
    return {"mode": "targeted", "removed": sorted(set(archived_ids))}


def _record_scope_gauge(store: MemoryStore) -> None:
    """Refresh `pheasant_memory_records{scope}` from the live store."""

    counts: dict[str, int] = {}
    for record in store.list_records():
        counts[record.scope] = counts.get(record.scope, 0) + 1
    for scope in ("session", "user", "org"):
        metrics.REGISTRY.set("pheasant_memory_records", float(counts.get(scope, 0)), scope=scope)


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
    # Re-globs the directory as it stands now (after `consolidate` above may
    # already have archived some records this same pass) — served mostly
    # from the parse-once cache, so this costs a `stat()` per live file
    # rather than a second full parse. A record `consolidate` already
    # archived this pass simply will not appear here (its file no longer
    # matches `mem-*.md`), so it is never archived twice.
    for record in store.list_records():
        if record.record_id in doomed:
            store.archive(record)
            archived.append(record.record_id)
    return sorted(archived)
