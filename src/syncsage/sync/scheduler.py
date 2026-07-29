"""Interval-based catch-all incremental sync loop.

The scheduler is the fallback freshness mechanism for sources the
filesystem watcher cannot observe (web collections, APIs, S3): a daemon
thread calls ``SyncEngine.sync_all(mode="incremental")`` every
``sync.scheduler.interval_seconds``. It is a no-op when disabled in config.

Scheduler- and watcher-triggered syncs are serialized through a shared
``sync_lock`` because ``SyncEngine`` is not safe for concurrent syncs within
one process (full crash/concurrency safety lands in Step 21.2).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from syncsage.config.schema import SyncSageConfig

if TYPE_CHECKING:
    from syncsage.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


class SchedulerService:
    """Daemon-thread interval loop running ``sync_all("incremental")``."""

    def __init__(
        self,
        engine: SyncEngine,
        config: SyncSageConfig | None = None,
        *,
        interval_seconds: float | None = None,
        enabled: bool | None = None,
        sync_lock: threading.Lock | None = None,
    ) -> None:
        self.engine = engine
        resolved = config if config is not None else getattr(engine, "config", None)
        settings = getattr(getattr(resolved, "sync", None), "scheduler", None)
        if interval_seconds is None:
            interval_seconds = getattr(settings, "interval_seconds", 900)
        if enabled is None:
            enabled = getattr(settings, "enabled", True)
        self.interval_seconds = float(interval_seconds)
        self.enabled = bool(enabled)
        self._sync_lock = sync_lock or threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or not self.enabled or self.interval_seconds <= 0:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="syncsage-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Scheduler started (interval=%ss)", self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
            self._thread = None
            logger.info("Scheduler stopped")

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self._sync_lock:
                    results = self.engine.sync_all("incremental")
                    # Step 33.2: memory consolidation rides the same beat —
                    # archive superseded/expired records, then a full re-sync
                    # of the (small) memory source drops them from the index.
                    # No-ops fast when no memory source is configured.
                    from syncsage.memory.maintenance import run_memory_maintenance

                    maintenance = run_memory_maintenance(self.engine)
                    # Step 32.4: the IdP group-sync refresh rides the same
                    # beat. No-ops when disabled or not yet due; a fetch
                    # failure is reported (never raised) so the beat
                    # survives and the mapping just ages toward the SLA.
                    from syncsage.security.idp import run_idp_maintenance

                    idp = run_idp_maintenance(self.engine.config, self.engine.state)
            except Exception:
                logger.exception("Scheduled incremental sync failed")
                continue
            if idp and not idp.get("error"):
                logger.debug("IdP group sync refreshed (%s principal(s))", idp["principals"])
            if maintenance and maintenance["report"]["archived"]:
                logger.info(
                    "Memory consolidation archived %s record(s)",
                    maintenance["report"]["archived"],
                )
            logger.debug("Scheduled sync completed for %s source(s)", len(results))
