"""Phase 35.6b — shedding load and draining cleanly.

Both behaviors exist for replicas and are off by default, so half of what is
tested here is that a single container is unaffected.
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.deployment.serving import (
    ALWAYS_ALLOWED,
    RETRY_AFTER_SECONDS,
    ConcurrencyLimiter,
    DrainState,
    install_drain_handler,
)


def _config(
    tmp_path: Path,
    *,
    state_name: str = "state",
    max_concurrent: int = 0,
    drain_seconds: int = 0,
) -> PheasantConfig:
    workspace = tmp_path / f"workspace-{state_name}"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.md").write_text(
        "# Note\n\nThe deployment gateway rotates credentials nightly.\n", encoding="utf-8"
    )
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "serving",
                "state_path": str(tmp_path / state_name),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / f"{state_name}-exports"),
            },
            "storage": {"graph_snapshots": False},
            "server": {
                "api": {
                    "max_concurrent_requests": max_concurrent,
                    "drain_seconds": drain_seconds,
                }
            },
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


# --------------------------------------------------------------------------
# The limiter
# --------------------------------------------------------------------------


def test_the_limiter_is_off_by_default(tmp_path: Path) -> None:
    """With one process there is nowhere for a shed request to go."""

    config = _config(tmp_path)
    assert config.server.api.max_concurrent_requests == 0

    limiter = ConcurrencyLimiter(0)
    assert limiter.enabled is False
    for _ in range(1000):
        assert limiter.acquire("/search") is True
    assert limiter.shed_total == 0


def test_the_limiter_admits_up_to_its_limit_then_sheds() -> None:
    limiter = ConcurrencyLimiter(2)

    assert limiter.acquire("/search") is True
    assert limiter.acquire("/search") is True
    assert limiter.acquire("/search") is False
    assert limiter.shed_total == 1

    limiter.release("/search")
    assert limiter.acquire("/search") is True
    assert limiter.inflight == 2
    assert limiter.peak_inflight == 2


def test_probe_routes_are_never_shed() -> None:
    """A pod that 429s its own liveness probe gets restarted by its protector.

    That turns a busy replica into a crash-looping one, which is strictly
    worse than the overload it was reacting to.
    """

    limiter = ConcurrencyLimiter(1)
    assert limiter.acquire("/search") is True

    for path in sorted(ALWAYS_ALLOWED):
        assert limiter.acquire(path) is True, f"{path} was shed"
    assert limiter.shed_total == 0
    # An exempt path must not consume a slot either, or a scrape would push a
    # real request over the limit.
    assert limiter.inflight == 1


def test_release_never_counts_below_zero() -> None:
    """An unpaired release would stop the limiter shedding at all.

    A bug that only shows up under the load the limiter exists for, so the
    floor is asserted rather than assumed.
    """

    limiter = ConcurrencyLimiter(1)
    limiter.release("/search")
    limiter.release("/search")
    assert limiter.inflight == 0
    assert limiter.acquire("/search") is True
    assert limiter.acquire("/search") is False


def test_the_limiter_is_thread_safe() -> None:
    limiter = ConcurrencyLimiter(4)
    admitted: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait(timeout=10)
        ok = limiter.acquire("/search")
        with lock:
            admitted.append(ok)
        if ok:
            time.sleep(0.02)
            limiter.release("/search")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sum(admitted) + limiter.shed_total == 16
    assert limiter.peak_inflight <= 4, "more requests were in flight than the limit allows"
    assert limiter.inflight == 0


# --------------------------------------------------------------------------
# Through the app
# --------------------------------------------------------------------------


def test_a_saturated_replica_answers_429_with_retry_after(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="shed", max_concurrent=1)
    app = create_app(config)
    client = TestClient(app)

    # Hold the single slot, then ask for another.
    assert app.state.limiter.acquire("/sources") is True
    response = client.get("/sources")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    assert "concurrency limit" in response.json()["detail"]

    app.state.limiter.release("/sources")
    assert client.get("/sources").status_code == 200


def test_probes_still_answer_on_a_saturated_replica(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="shed-probe", max_concurrent=1)
    app = create_app(config)
    client = TestClient(app)

    assert app.state.limiter.acquire("/sources") is True
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/sources").status_code == 429


def test_health_ready_metrics_are_declared_async(tmp_path: Path) -> None:
    """A2's actual, unconditional fix: these three routes must not dispatch
    through anyio's thread pool at all.

    Checked structurally rather than by timing. Timing is the right tool for
    proving `/health` specifically answers instantly under saturation (see
    the test below) but is the *wrong* tool for `/ready` and `/metrics`:
    both still make one blocking call each (``state.rows``, ``queue.depth``),
    so under literal 100% pool saturation they queue for a token exactly as
    long in the old, buggy code as in the new, fixed code — the fix there is
    that only that one call touches the pool, not the whole route, which a
    stopwatch cannot see but ``inspect.iscoroutinefunction`` can.
    """

    import inspect

    from fastapi.routing import APIRoute

    config = _config(tmp_path, state_name="async-routes")
    app = create_app(config)

    endpoints = {
        route.path: route.endpoint
        for route in app.routes
        if isinstance(route, APIRoute) and route.path in {"/health", "/ready", "/metrics"}
    }
    assert endpoints.keys() == {"/health", "/ready", "/metrics"}
    for path, endpoint in endpoints.items():
        assert inspect.iscoroutinefunction(endpoint), f"{path} is not an async def route"


@pytest.mark.asyncio
async def test_health_answers_instantly_under_full_thread_pool_saturation(
    tmp_path: Path,
) -> None:
    """`/health` needs zero worker-thread tokens, provably.

    `test_probes_still_answer_on_a_saturated_replica` occupies a limiter
    *count* via ``limiter.acquire()`` directly — it never touches a real
    worker thread, so it would pass identically even if `/health` were sync
    ``def`` and queued behind other in-flight sync handlers on anyio's
    shared thread pool. This test saturates that actual pool — every single
    token, held indefinitely — and proves `/health` still answers, because
    unlike `/ready` and `/metrics` it makes no blocking call at all and so
    never asks the pool for anything.

    Uses ``httpx.AsyncClient`` + ``ASGITransport`` rather than
    ``TestClient``: ``TestClient`` runs the app on its own blocking portal —
    a separate event loop on a separate thread — so
    ``anyio.to_thread.current_default_thread_limiter()`` called from this
    test body would not be the same limiter instance the app's route
    handlers see. ``ASGITransport`` calls the app in-process on the calling
    coroutine's own event loop, so the test and the app share one pool.
    """

    import asyncio

    import anyio.to_thread
    from httpx import ASGITransport, AsyncClient

    config = _config(tmp_path, state_name="health-under-saturation")
    app = create_app(config)

    pool = anyio.to_thread.current_default_thread_limiter()
    original_total = pool.total_tokens
    pool_size = 4  # small and deterministic, not anyio's real default of 40
    pool.total_tokens = pool_size

    starts = [threading.Event() for _ in range(pool_size)]
    release = threading.Event()

    def _hog(start_evt: threading.Event) -> None:
        start_evt.set()
        release.wait(timeout=10)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hog_tasks = [asyncio.create_task(anyio.to_thread.run_sync(_hog, evt)) for evt in starts]
            deadline = time.monotonic() + 5
            while not all(evt.is_set() for evt in starts):
                assert time.monotonic() < deadline, "hog threads never acquired all pool tokens"
                await asyncio.sleep(0.01)
            # Every token is now held, indefinitely (until release.set()) —
            # the pool is fully saturated with nothing free for anyone else.

            start = time.monotonic()
            response = await client.get("/health")
            elapsed = time.monotonic() - start
            assert response.status_code == 200
            assert elapsed < 1.0, f"/health took {elapsed:.2f}s under a fully saturated pool"
        release.set()
        await asyncio.gather(*hog_tasks)
    finally:
        release.set()
        pool.total_tokens = original_total


def test_an_unlimited_replica_never_sheds(tmp_path: Path) -> None:
    """Rule 7 in serving form: the default path is untouched."""

    config = _config(tmp_path, state_name="unlimited")
    app = create_app(config)
    client = TestClient(app)

    for _ in range(20):
        assert client.get("/sources").status_code == 200
    assert app.state.limiter.shed_total == 0


def _shed_count(body: str, path: str) -> float:
    """Read one labelled counter out of the exposition text.

    A delta, not an absolute: the metrics registry is a process-global
    singleton, so counters carry over between tests in one run. Asserting an
    absolute value here passes in isolation and fails in the suite — which is
    exactly how it first failed.
    """

    needle = f'pheasant_requests_shed_total{{path="{path}"}} '
    for line in body.splitlines():
        if line.startswith(needle):
            return float(line[len(needle) :])
    return 0.0


def test_shedding_is_reported_in_metrics(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="shed-metrics", max_concurrent=1)
    app = create_app(config)
    client = TestClient(app)
    before = _shed_count(client.get("/metrics").text, "/sources")

    app.state.limiter.acquire("/sources")
    client.get("/sources")
    app.state.limiter.release("/sources")

    body = client.get("/metrics").text
    assert _shed_count(body, "/sources") == before + 1
    assert "pheasant_requests_inflight 0" in body
    assert "pheasant_draining 0" in body


def test_metric_paths_are_collapsed_to_one_segment(tmp_path: Path) -> None:
    """A label per artifact id is a memory leak in the scrape target."""

    config = _config(tmp_path, state_name="cardinality", max_concurrent=1)
    app = create_app(config)
    client = TestClient(app)
    before = _shed_count(client.get("/metrics").text, "/sources")

    app.state.limiter.acquire("/x")
    for suffix in ("alpha", "beta", "gamma"):
        client.get(f"/sources/{suffix}/history")
    app.state.limiter.release("/x")

    body = client.get("/metrics").text
    assert _shed_count(body, "/sources") == before + 3
    assert "alpha" not in body and "beta" not in body


# --------------------------------------------------------------------------
# Draining
# --------------------------------------------------------------------------


def test_drain_state_tracks_when_it_began() -> None:
    state = DrainState()
    assert state.draining is False
    assert state.draining_for == 0.0

    assert state.begin() is True
    assert state.draining is True
    # A second SIGTERM is an operator saying "stop now", not a restart of the
    # clock.
    assert state.begin() is False
    assert state.draining_for >= 0.0

    state.reset()
    assert state.draining is False


def test_ready_reports_503_while_draining_but_health_stays_green(tmp_path: Path) -> None:
    """Kubernetes uses the two probes for different things.

    Readiness removes the pod from the Service — which is what draining wants.
    Liveness restarts it, which during a graceful shutdown would be absurd.
    """

    config = _config(tmp_path, state_name="draining")
    app = create_app(config)
    client = TestClient(app)
    assert client.get("/ready").status_code == 200

    app.state.drain.begin()

    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["status"] == "draining"
    assert ready.json()["draining_for_seconds"] >= 0
    assert client.get("/health").status_code == 200
    assert "pheasant_draining 1" in client.get("/metrics").text


def test_a_draining_replica_still_serves_requests(tmp_path: Path) -> None:
    """Draining is 'stop being sent work', not 'stop doing work'.

    The whole point is to keep answering what was already routed here while
    the load balancer catches up.
    """

    config = _config(tmp_path, state_name="drain-serve")
    app = create_app(config)
    client = TestClient(app)

    app.state.drain.begin()
    assert client.get("/sources").status_code == 200


def test_the_drain_handler_waits_then_chains_to_the_previous_handler() -> None:
    """The chain is what stops a drain becoming a hang.

    Replacing uvicorn's handler rather than wrapping it would drain and then
    never exit — the same dropped requests, arrived at more slowly, and ending
    in SIGKILL.
    """

    state = DrainState()
    waits: list[float] = []
    chained: list[int] = []
    pending: list[Any] = []

    previous = signal.getsignal(signal.SIGUSR1)
    try:
        signal.signal(signal.SIGUSR1, lambda signum, frame: chained.append(signum))
        install_drain_handler(
            state,
            3.0,
            signals=[signal.SIGUSR1],
            defer=lambda seconds, callback: (waits.append(seconds), pending.append(callback)),
        )

        signal.raise_signal(signal.SIGUSR1)
        time.sleep(0.05)

        assert state.draining is True
        assert waits == [3.0]
        assert chained == [], "the handler chained immediately instead of after the drain"
        pending[0]()
        assert chained == [signal.SIGUSR1], "the previous handler was replaced, not wrapped"
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_the_drain_handler_returns_immediately_rather_than_sleeping() -> None:
    """The wait belongs on a timer thread, not in the handler.

    Signal handlers run on the main thread — for an ASGI server that is the
    event loop. Sleeping there stops the process answering anything for the
    whole drain window, including the `/ready` 503 the load balancer is
    waiting for, which is strictly worse than not draining at all. The real
    (non-injected) `defer` is used here, so this fails if the production path
    ever goes back to sleeping.
    """

    state = DrainState()
    chained = threading.Event()
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        signal.signal(signal.SIGUSR1, lambda signum, frame: chained.set())
        install_drain_handler(state, 0.2, signals=[signal.SIGUSR1])

        started = time.monotonic()
        signal.raise_signal(signal.SIGUSR1)
        handler_returned = time.monotonic() - started

        assert handler_returned < 0.1, "the handler blocked for the drain window"
        assert state.draining is True
        assert not chained.is_set(), "shutdown was chained before the drain elapsed"
        assert chained.wait(3.0), "the deferred chain never ran"
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_a_second_signal_skips_the_wait() -> None:
    state = DrainState()
    waits: list[float] = []
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        signal.signal(signal.SIGUSR1, lambda signum, frame: None)
        install_drain_handler(
            state,
            5.0,
            signals=[signal.SIGUSR1],
            defer=lambda seconds, callback: waits.append(seconds),
        )
        signal.raise_signal(signal.SIGUSR1)
        signal.raise_signal(signal.SIGUSR1)
        time.sleep(0.05)

        assert waits == [5.0], "the second signal waited again"
    finally:
        signal.signal(signal.SIGUSR1, previous)


def _record_installs(monkeypatch: Any) -> list[tuple[Any, float]]:
    """Capture `install_drain_handler` calls made by the lifespan.

    The call is asserted rather than the resulting `signal.getsignal`, because
    `TestClient` runs the lifespan off the main thread, where `signal.signal`
    cannot be called at all. That is a property of the test harness, not of
    production — and the thing that was actually wrong before was *where* the
    installation happened, which is exactly what this catches.
    """

    from pheasant.deployment import serving

    calls: list[tuple[Any, float]] = []
    monkeypatch.setattr(
        serving,
        "install_drain_handler",
        lambda state, seconds, **kwargs: calls.append((state, seconds)),
    )
    return calls


def test_no_drain_handler_is_installed_when_the_delay_is_zero(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Default off: a laptop must not pause on Ctrl-C."""

    config = _config(tmp_path, state_name="no-drain")
    assert config.server.api.drain_seconds == 0

    calls = _record_installs(monkeypatch)
    with TestClient(create_app(config)):
        pass
    assert calls == []


def test_the_drain_handler_is_installed_from_the_lifespan(tmp_path: Path, monkeypatch: Any) -> None:
    """Installed in lifespan startup, not before `uvicorn.run()`.

    uvicorn installs its own graceful-shutdown handler inside `run()`, so a
    handler installed earlier is simply replaced and never fires. Lifespan
    startup runs after that, which is what lets ours chain — the reason this
    test asserts *where* the call happens.
    """

    config = _config(tmp_path, state_name="drain-install", drain_seconds=2)
    calls = _record_installs(monkeypatch)
    app = create_app(config)
    with TestClient(app):
        pass

    assert len(calls) == 1
    state, seconds = calls[0]
    assert state is app.state.drain
    assert seconds == 2.0


def test_max_concurrent_requests_raises_the_thread_pool_budget(tmp_path: Path) -> None:
    """A4: `max_concurrent_requests` and anyio's thread pool are separate
    budgets by default (40 tokens, unrelated to the limiter's own count).
    Wiring them together at lifespan startup is what keeps a request the
    limiter admits from silently queuing for a thread instead — otherwise
    setting `max_concurrent_requests` above the pool size reintroduces
    exactly the blocking behavior shedding exists to replace.

    Reads the pool via ``client.portal`` (available once inside
    ``with TestClient(app) as client:``) rather than
    ``anyio.to_thread.current_default_thread_limiter()`` called directly
    from this test body: that call needs a running event loop in the
    *current* thread, and this is a plain sync test with none — the pool
    that matters here lives on TestClient's own portal thread, which is
    also where the lifespan that raises it actually ran.
    """

    import anyio.to_thread

    config = _config(tmp_path, state_name="pool-budget", max_concurrent=80)
    app = create_app(config)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        total = client.portal.call(
            lambda: anyio.to_thread.current_default_thread_limiter().total_tokens
        )
    assert total >= 80, "the pool was not raised to match max_concurrent_requests"


def test_installing_off_the_main_thread_does_not_break_startup(tmp_path: Path) -> None:
    """A real install attempt in a worker thread raises; startup must survive.

    `TestClient` is that case, and so is any embedding host that starts the
    app from a thread. Failing to install a drain handler is a degraded
    deployment, not a broken one.
    """

    config = _config(tmp_path, state_name="drain-thread", drain_seconds=2)
    with TestClient(create_app(config)) as client:
        assert client.get("/health").status_code == 200


# --------------------------------------------------------------------------
# The property that makes MCP replicas safe
# --------------------------------------------------------------------------


def test_the_mcp_server_is_stateless(tmp_path: Path) -> None:
    """Pinned because it is load-bearing and was previously undocumented.

    With no per-session server state, two requests from one agent may land on
    different replicas and both answer correctly. A sticky session would make
    the replica count a lie, and nothing else in the suite would notice.
    """

    pytest.importorskip("mcp", reason="the [mcp] extra is optional")

    from pheasant.mcp_server.server import streamable_http_app

    config = _config(tmp_path, state_name="mcp")

    # Read off the app pheasant actually mounts, not off a settings object:
    # MCP SDK 2.x moved transport configuration onto `streamable_http_app()`
    # and deleted the constructor fields this used to assert on, so a server
    # built without those keywords is a *stateful* one that still passes any
    # assertion made against the constructor.
    app = streamable_http_app(config)
    assert app is not None, "the streamable-http transport is on by default"
    session_manager = app.routes[0].endpoint.session_manager
    assert session_manager.stateless is True
    assert session_manager.json_response is True


def test_the_mcp_instructions_do_not_advertise_a_removed_tool(tmp_path: Path) -> None:
    """The Obsidian exporter went in 35.0; the instructions still named it."""

    pytest.importorskip("mcp", reason="the [mcp] extra is optional")

    from pheasant.mcp_server.server import create_mcp_server

    server = create_mcp_server(_config(tmp_path, state_name="mcp-instructions"))
    assert "obsidian" not in (server.instructions or "").lower()
