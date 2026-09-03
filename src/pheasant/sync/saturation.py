"""How full the single commit authority is (Phase 35.8).

One indexer is the sole commit authority for a knowledge base. Extra indexers
for one shard are elected hot standbys, not throughput replicas: the graph,
the vectors and the graph FTS are one coordinated commit stream, and the whole
design of the region rests on that being true.

The ceiling is defensible. What was not defensible is that it was **prose**.
A team scales workers, watches ingest stop improving, and has nothing telling
them they have reached the commit-authority limit rather than a tuning
problem — because the two look identical from the outside: the queue drains
more slowly than work arrives, and adding workers changes nothing.

So the ceiling is published as a number:

    pheasant_commit_authority_saturation   0.0 .. 1.0

the fraction of a rolling window this process spent indexing. Sustained above
:data:`SHARD_THRESHOLD` means the commit authority is the bottleneck and more
workers will not help — shard (`pheasant shard plan`). Below it, a slow queue
is a tuning problem, and the tuning plane is where to take it.

Three things it deliberately is not:

**Not a queue-depth gauge.** Depth already exists
(``pheasant_index_queue_depth``) and answers a different question: it says
work is waiting, not *why*. A deep queue behind an idle indexer is a claim
problem; a deep queue behind a saturated one is the ceiling.

**Not an average since boot.** A region that indexed hard this morning and is
idle now would report itself saturated all afternoon, which is exactly when
somebody would be reading it. The window is rolling.

**Not published below a minimum observation.** Two seconds of indexing in the
first four seconds of a pod's life is not 50% saturation, and a gauge that
says so invites sharding a region that is doing nothing. Under
:data:`MINIMUM_WINDOW_SECONDS` of observed wall time this reports ``None`` and
the scrape omits the series — the same posture ``tuning.health`` takes below
its sample floor, and for the same reason.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

#: Sustained above this, the commit authority is the bottleneck. Chosen, not
#: measured, and openly so: it is the point past which a *queueing* system's
#: waiting time starts climbing steeply (utilisation 0.8 roughly quintuples
#: the wait over an idle system), so it is where "busy" turns into "the thing
#: everything else is waiting for" rather than a threshold of pheasant's own.
SHARD_THRESHOLD = 0.8

#: The rolling window. Five minutes is long enough that one large source's
#: commit does not read as saturation, and short enough that a reader watching
#: a dashboard sees the region's current state rather than its morning.
WINDOW_SECONDS = 300.0

#: Below this much observed wall time, publish nothing.
MINIMUM_WINDOW_SECONDS = 60.0


class CommitAuthorityMeter:
    """Busy fraction of the process that owns the commit stream.

    Thread-safe because an indexer runs several sources through a pool: the
    intervals overlap, and a naive sum of durations would report 300% busy on
    a three-source pass. Overlapping intervals are merged, so this measures
    *wall time in which the commit authority was doing something*, which is
    the quantity that cannot exceed one.
    """

    def __init__(
        self,
        *,
        window_seconds: float = WINDOW_SECONDS,
        minimum_seconds: float = MINIMUM_WINDOW_SECONDS,
        clock: object | None = None,
    ) -> None:
        self.window = float(window_seconds)
        self.minimum = float(minimum_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._spans: deque[tuple[float, float]] = deque()
        self._started_at = float(self._clock())
        self._open: dict[int, float] = {}

    @contextmanager
    def busy(self) -> Iterator[None]:
        """Mark this thread as doing commit-authority work for the duration."""

        key = threading.get_ident()
        started = float(self._clock())
        with self._lock:
            # Re-entrant by design: `sync_all` calls `sync_source`. The outer
            # span is the one that counts, and the inner one must not restart
            # the clock or the nested exit would close a span that is still
            # open.
            nested = key in self._open
            if not nested:
                self._open[key] = started
        try:
            yield
        finally:
            if not nested:
                with self._lock:
                    opened = self._open.pop(key, started)
                    self._spans.append((opened, float(self._clock())))
                    self._trim()

    def _trim(self) -> None:
        horizon = float(self._clock()) - self.window
        while self._spans and self._spans[0][1] <= horizon:
            self._spans.popleft()

    def saturation(self) -> float | None:
        """Busy fraction over the window, or None when there is too little of it."""

        now = float(self._clock())
        with self._lock:
            self._trim()
            horizon = now - self.window
            spans = [(max(start, horizon), end) for start, end in self._spans if end > horizon]
            # An interval still open right now counts up to this instant --
            # otherwise a single multi-hour source reports zero saturation for
            # its whole duration, which is precisely when it matters.
            spans.extend((max(start, horizon), now) for start in self._open.values())
            observed = min(self.window, now - self._started_at)
        if observed < self.minimum:
            return None
        return round(min(1.0, _merged_seconds(spans) / observed), 4)


def _merged_seconds(spans: list[tuple[float, float]]) -> float:
    """Union of possibly-overlapping intervals, in seconds."""

    total = 0.0
    end_of_run = None
    for start, end in sorted(spans):
        if end <= start:
            continue
        if end_of_run is None or start > end_of_run:
            total += end - start
            end_of_run = end
        elif end > end_of_run:
            total += end - end_of_run
            end_of_run = end
    return total
