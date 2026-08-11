"""Background work you can watch: one registry for every long-running job.

A first index of a real repository takes minutes. Before this, the only thing
the UI could say about that was a boolean — ``syncing: true`` on a source row,
flipping to nothing when the sync finished — which is indistinguishable from a
hang for as long as it runs. That is the gap this module closes: every
background job gets an id, a phase, a counter, a tail of what it last did, and
a terminal outcome that survives the job itself.

Deliberately **in-process and in-memory**, like the ad-hoc
``syncing_sources``/``sync_outcomes`` dicts it replaces:

* Jobs are a property of a running server, not of the knowledge base. Writing
  them to ``/state`` would make them user data (CLAUDE.md §4 rule 2) and buy
  nothing — a job cannot outlive the process that is running it, so a restart
  invalidates every record anyway.
* It keeps the indexing path free of another writer. ``/state`` has one
  single-writer lease for good reasons.

Bounded on purpose: ``max_records`` caps history and each job keeps only a
tail of its log, so a server that syncs on a timer for a month does not grow a
job list for a month.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from queue import Empty, Queue
from typing import Any

from pheasant.ingestion.pipeline import utc_now

#: Terminal states. A job in any of these will never change again.
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

#: How many completed jobs to keep. Running jobs are never evicted.
DEFAULT_MAX_RECORDS = 200

#: Lines of a job's output kept for the UI. The full output goes to the log.
LOG_TAIL = 40


@dataclass
class JobProgress:
    """Where a job has got to.

    ``total`` is ``None`` until it is known — a sync does not know how many
    files it will index until the connector has finished listing them, and
    inventing a denominator so the bar looks nicer would make it lie.
    """

    phase: str = "starting"
    current: int = 0
    total: int | None = None
    detail: str = ""

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.current / self.total)


@dataclass
class Job:
    id: str
    kind: str
    label: str
    targets: list[str] = field(default_factory=list)
    status: str = "queued"
    progress: JobProgress = field(default_factory=JobProgress)
    started_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["log"] = list(self.log)
        payload["progress"]["fraction"] = self.progress.fraction
        payload["active"] = self.status not in TERMINAL
        return payload


class JobRegistry:
    """Thread-safe registry of background jobs, with a subscription stream.

    Every mutation is under one lock and every reader gets a snapshot: jobs
    are written from worker threads and read from request threads, and handing
    out a live object would let a response serialize a half-updated record.
    """

    def __init__(self, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._max_records = max_records
        self._subscribers: list[Queue] = []

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        kind: str,
        label: str,
        targets: list[str] | None = None,
        *,
        job_id: str | None = None,
    ) -> Job:
        job = Job(
            id=job_id or uuid.uuid4().hex[:12],
            kind=kind,
            label=label,
            targets=list(targets or []),
            status="running",
            started_at=utc_now(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict()
        self._publish(job)
        return job

    def progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Record forward movement. Unknown job ids are ignored, not raised.

        A progress callback fires from deep inside the indexing loop; making
        it capable of failing a sync — because a job was evicted, or the
        registry was swapped in a test — would be a poor trade for a
        cosmetic feature.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL:
                return
            if phase is not None:
                job.progress.phase = phase
            if current is not None:
                job.progress.current = current
            if total is not None:
                job.progress.total = total
            if detail is not None:
                job.progress.detail = detail
                job.log.append(f"{utc_now()} {detail}")
            snapshot = job
        self._publish(snapshot)

    def finish(
        self,
        job_id: str,
        status: str = "succeeded",
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.error = error
            job.result = result
            job.finished_at = utc_now()
            job.progress.phase = status
            if job.progress.total:
                # Finish the bar. A job that succeeded while its counter still
                # reads 812/1000 (because the last files were skipped, not
                # indexed) reads as "stopped early".
                job.progress.current = job.progress.total
            snapshot = job
        self._publish(snapshot)

    # -- reading ----------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.as_dict() if job else None

    def list(self, *, active_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. Running jobs always sort ahead of finished ones."""
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]
        if active_only:
            jobs = [job for job in jobs if job.status not in TERMINAL]
        jobs.sort(key=lambda job: (job.status in TERMINAL, job.started_at), reverse=False)
        active = [job for job in jobs if job.status not in TERMINAL]
        done = [job for job in jobs if job.status in TERMINAL]
        active.reverse()
        done.reverse()
        return [job.as_dict() for job in (active + done)[:limit]]

    def active_for(self, target: str) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.status not in TERMINAL and target in job.targets:
                    return job
        return None

    def last_outcome_for(self, target: str) -> Job | None:
        """The most recent *finished* job that touched ``target``.

        The UI needs this because ``active`` alone flickers to nothing the
        instant a job completes — which is precisely when a caller most wants
        to know whether it succeeded.
        """
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.status in TERMINAL and target in job.targets:
                    return job
        return None

    def clear(self, job_id: str | None = None) -> int:
        """Remove finished notifications; active work is never cancelled."""
        with self._lock:
            selected = [job_id] if job_id is not None else list(self._order)
            removed = 0
            for candidate in selected:
                job = self._jobs.get(candidate)
                if job is not None and job.status in TERMINAL:
                    self._jobs.pop(candidate, None)
                    removed += 1
            if removed:
                self._order = [candidate for candidate in self._order if candidate in self._jobs]
            return removed

    # -- streaming --------------------------------------------------------

    def subscribe(self) -> Queue:
        queue: Queue = Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def drain(self, queue: Queue) -> list[dict[str, Any]]:
        """Everything waiting on a subscriber's queue, without blocking.

        Deliberately non-blocking. A ``queue.get(timeout=…)`` helper used to
        live here and it was a trap: Starlette runs a sync generator in a
        threadpool it cannot interrupt, so an SSE client that disconnected
        left a thread blocked on a queue nobody would ever write to again —
        one leaked thread per dropped connection. Consumers poll this from an
        async loop instead, which cancels cleanly.
        """
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(queue.get_nowait())
            except Empty:
                return items

    def _publish(self, job: Job) -> None:
        payload = job.as_dict()
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(payload)
            except Exception:
                # A subscriber that cannot keep up loses updates rather than
                # blocking the job that is producing them. The next update, and
                # the terminal one, still arrive.
                pass

    def _evict(self) -> None:
        """Drop the oldest finished jobs past the cap. Lock held."""
        if len(self._order) <= self._max_records:
            return
        keep: list[str] = []
        removable = len(self._order) - self._max_records
        for job_id in self._order:
            job = self._jobs.get(job_id)
            if removable > 0 and job is not None and job.status in TERMINAL:
                self._jobs.pop(job_id, None)
                removable -= 1
                continue
            keep.append(job_id)
        self._order = keep
