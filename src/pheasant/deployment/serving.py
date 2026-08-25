"""Serving durability: shedding load and draining cleanly (Phase 35.6).

Two behaviors a replica needs and a single container does not.

**Shed rather than queue.** With one process, a burst of expensive requests
piles up behind the GIL and every one of them gets slower — but there is
nowhere else for them to go, so waiting is the best available answer. With N
replicas behind a load balancer there *is* somewhere else, and a 429 with
``Retry-After`` is strictly better than a request that sits for thirty
seconds and then times out anyway. That is why the limit defaults to **off**:
it is not a safety feature that everyone should want, it is a routing hint
that only means something when there is a router.

**Drain before dying.** Kubernetes sends SIGTERM and removes the pod from
Service endpoints *concurrently*, and endpoint propagation is not instant —
kube-proxy on every node has to be told. A process that exits promptly on
SIGTERM therefore drops the requests that were routed to it in the gap. The
fix is not to exit faster: it is to fail readiness *first*, keep serving for
a moment, and only then shut down.

Probe routes are never shed and never fail on saturation. A pod that answers
429 to its own liveness probe gets **restarted** by the thing meant to be
protecting it, turning a busy replica into a crash-looping one — a strictly
worse outcome than the overload it was reacting to.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Never shed, never drained: liveness, readiness and scrapes. The first two
#: because failing them escalates a load problem into a restart; `/metrics`
#: because losing observability exactly when a replica is saturated is losing
#: it when it matters.
ALWAYS_ALLOWED: frozenset[str] = frozenset({"/health", "/ready", "/metrics"})

#: What a shed response tells the caller to do. One second: the point is to
#: make the client come back or, better, to make a load balancer try another
#: replica — not to impose a penalty.
RETRY_AFTER_SECONDS = 1


class ConcurrencyLimiter:
    """Count in-flight requests and refuse the surplus.

    A counter under a lock rather than a semaphore: nothing here ever
    *blocks*, because blocking is the behavior being replaced. A request is
    either admitted immediately or refused immediately, which is what makes
    the 429 useful — it arrives fast enough for a load balancer to retry it
    somewhere else while the client is still waiting.
    """

    def __init__(self, limit: int, *, exempt: Iterable[str] = ALWAYS_ALLOWED) -> None:
        self.limit = max(0, int(limit))
        self.exempt = frozenset(exempt)
        self._lock = threading.Lock()
        self._inflight = 0
        self.shed_total = 0
        self.peak_inflight = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    @property
    def capacity_remaining(self) -> int:
        """Immediately available request slots; 0 when limiting is disabled."""

        with self._lock:
            return max(0, self.limit - self._inflight) if self.enabled else 0

    def is_exempt(self, path: str) -> bool:
        return path in self.exempt

    def acquire(self, path: str) -> bool:
        """True when the request may proceed; False when it should be shed."""

        if not self.enabled or self.is_exempt(path):
            return True
        with self._lock:
            if self._inflight >= self.limit:
                self.shed_total += 1
                return False
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
            return True

    def release(self, path: str) -> None:
        if not self.enabled or self.is_exempt(path):
            return
        with self._lock:
            # Floor at zero rather than trusting pairing: a middleware that
            # releases a request it never admitted would otherwise make the
            # limiter count backwards and stop shedding entirely — a bug that
            # only shows up under the load the limiter exists for.
            self._inflight = max(0, self._inflight - 1)


class DrainState:
    """Whether this process is shutting down, and since when.

    Shared between the signal handler and `/ready`, which is the whole
    mechanism: the handler flips it, the probe reports it, the orchestrator
    removes the endpoint, and only then does the process stop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._draining_since: float | None = None

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._draining_since is not None

    @property
    def draining_for(self) -> float:
        with self._lock:
            if self._draining_since is None:
                return 0.0
            return max(0.0, time.monotonic() - self._draining_since)

    def begin(self) -> bool:
        """Mark draining. False when it had already begun (a second SIGTERM)."""

        with self._lock:
            if self._draining_since is not None:
                return False
            self._draining_since = time.monotonic()
            return True

    def reset(self) -> None:
        with self._lock:
            self._draining_since = None


def _defer(seconds: float, callback) -> None:
    """Run ``callback`` after ``seconds`` on a daemon timer thread."""

    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()


def install_drain_handler(
    state: DrainState,
    drain_seconds: float,
    *,
    signals: Iterable[int] | None = None,
    defer=_defer,
) -> None:
    """Fail readiness on SIGTERM, wait, then let the server shut down.

    The handler chains to whatever was installed before it — uvicorn installs
    its own graceful-shutdown handler — rather than replacing it. Replacing it
    would drain and then never exit, which reads as a hung pod and ends in
    SIGKILL: the same dropped requests, arrived at more slowly.

    The wait is **deferred to a timer thread, never slept in the handler**.
    Python runs signal handlers on the main thread, between bytecodes of
    whatever it was doing — for an ASGI server that is the event loop itself.
    Sleeping there stops the loop, so for the whole drain window the process
    answers nothing: not the in-flight requests the drain exists to finish,
    not `/ready` (whose flip to 503 is the signal the load balancer is waiting
    for), not `/health` (so a liveness probe can conclude the pod is dead and
    escalate to SIGKILL). It would have inverted the feature — a graceful
    shutdown that drops strictly more requests than an abrupt one.

    A **second** SIGTERM skips the wait. An operator sending it twice is
    saying "stop now", and honouring the delay anyway would be ignoring them.
    """

    import signal as signal_module

    targets = list(signals) if signals is not None else [signal_module.SIGTERM]
    for number in targets:
        previous = signal_module.getsignal(number)

        def chain(signum, frame, _previous):  # type: ignore[no-untyped-def]
            if callable(_previous):
                _previous(signum, frame)
            elif _previous == signal_module.SIG_DFL:  # pragma: no cover - no server attached
                signal_module.signal(signum, signal_module.SIG_DFL)
                signal_module.raise_signal(signum)

        def handler(signum, frame, _previous=previous):  # type: ignore[no-untyped-def]
            first = state.begin()
            if first and drain_seconds > 0:
                logger.info(
                    "SIGTERM: reporting not-ready and draining for %.0fs before shutdown",
                    drain_seconds,
                )
                defer(drain_seconds, lambda: chain(signum, frame, _previous))
                return
            if not first:
                logger.info("second termination signal: shutting down without draining")
            chain(signum, frame, _previous)

        signal_module.signal(number, handler)
