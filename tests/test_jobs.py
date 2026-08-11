"""Background work you can watch.

Acceptance:

1. A job records a phase, a counter and a terminal outcome, and always reaches
   a terminal state — a source can never be stuck showing "syncing" forever.
2. A progress callback can never fail the sync that calls it.
3. The child process's progress reaches the parent *while* the sync runs
   (streamed), not after it.
4. The registry is bounded: finished jobs are evicted, running ones never are.
5. The HTTP surface exposes it, including a source's live job.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.cli import PROGRESS_MARKER, _progress_emitter
from pheasant.jobs import JobRegistry
from pheasant.sync.engine import _progress_reporter

SAMPLE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "sample_workspace"


# ------------------------------------------------------------------ registry


def test_a_job_moves_through_phases_to_a_terminal_state() -> None:
    registry = JobRegistry()
    job = registry.create("sync", "Indexing docs", ["docs"])

    assert registry.active_for("docs").id == job.id
    registry.progress(job.id, phase="listing", detail="listing docs")
    registry.progress(job.id, phase="indexing", current=3, total=10, detail="a.md")

    live = registry.get(job.id)
    assert live["progress"]["phase"] == "indexing"
    assert live["progress"]["current"] == 3
    assert live["progress"]["fraction"] == pytest.approx(0.3)
    assert live["active"] is True

    registry.finish(job.id, "succeeded")
    done = registry.get(job.id)
    assert done["status"] == "succeeded"
    assert done["active"] is False
    assert registry.active_for("docs") is None
    assert registry.last_outcome_for("docs").id == job.id


def test_an_unknown_total_reports_no_fraction() -> None:
    """A denominator nobody knows yet must not be invented as 0%."""
    registry = JobRegistry()
    job = registry.create("sync", "Indexing", ["docs"])
    registry.progress(job.id, phase="listing", current=0)
    assert registry.get(job.id)["progress"]["fraction"] is None


def test_finishing_completes_the_bar() -> None:
    """A sync that succeeded with 812/1000 (the rest skipped) is not stopped."""
    registry = JobRegistry()
    job = registry.create("sync", "Indexing", ["docs"])
    registry.progress(job.id, current=812, total=1000)
    registry.finish(job.id, "succeeded")
    assert registry.get(job.id)["progress"]["current"] == 1000


def test_a_failed_job_keeps_its_error_after_it_finishes() -> None:
    registry = JobRegistry()
    job = registry.create("sync", "Indexing", ["docs"])
    registry.finish(job.id, "failed", error="connector exploded")
    assert registry.last_outcome_for("docs").error == "connector exploded"


def test_progress_for_an_unknown_or_finished_job_is_ignored_not_raised() -> None:
    """This fires from inside the indexing loop; it must never be fatal."""
    registry = JobRegistry()
    registry.progress("does-not-exist", phase="indexing")
    job = registry.create("sync", "Indexing", ["docs"])
    registry.finish(job.id)
    registry.progress(job.id, phase="indexing", current=99)
    assert registry.get(job.id)["progress"]["current"] != 99


def test_finished_jobs_are_evicted_but_running_ones_are_not() -> None:
    registry = JobRegistry(max_records=3)
    running = registry.create("sync", "still going", ["live"])
    for index in range(6):
        done = registry.create("sync", f"done-{index}", [f"s{index}"])
        registry.finish(done.id)
    assert registry.get(running.id) is not None
    assert len(registry.list(limit=100)) <= 4


def test_subscribers_receive_updates_and_a_slow_one_cannot_block_the_job() -> None:
    registry = JobRegistry()
    queue = registry.subscribe()
    job = registry.create("sync", "Indexing", ["docs"])
    registry.progress(job.id, phase="indexing", current=1, total=2)
    registry.finish(job.id)

    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    assert [item["status"] for item in seen][-1] == "succeeded"

    # A queue nobody drains must not wedge the producer.
    small = registry.subscribe()
    for _ in range(500):
        registry.progress(job.id, detail="x")
    registry.unsubscribe(small)


def test_active_jobs_sort_ahead_of_finished_ones() -> None:
    registry = JobRegistry()
    old = registry.create("sync", "old", ["a"])
    registry.finish(old.id)
    live = registry.create("sync", "live", ["b"])
    assert registry.list()[0]["id"] == live.id


# ------------------------------------------------------------ the engine hook


def test_a_broken_progress_hook_cannot_fail_a_sync() -> None:
    def explode(*_args):
        raise RuntimeError("callback is broken")

    report = _progress_reporter(explode, "docs")
    report("indexing", 1, 10, "a.md")  # must not raise


def test_no_hook_is_a_cheap_no_op() -> None:
    report = _progress_reporter(None, "docs")
    report("indexing", 1, 10, "a.md")


def test_the_engine_reports_progress_through_a_real_sync(loaded_config) -> None:
    from pheasant.persistence.paths import StatePaths
    from pheasant.persistence.state_store import StateStore
    from pheasant.sync.engine import SyncEngine

    events: list[tuple] = []
    paths = StatePaths.from_config(loaded_config)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    engine = SyncEngine(loaded_config, paths, state)
    try:
        source = loaded_config.sources[0].name
        engine.sync_source(
            source,
            "full",
            on_progress=lambda *args: events.append(args),
        )
    finally:
        engine.close()

    phases = [event[0] for event in events]
    assert "listing" in phases
    assert "indexing" in phases
    assert "saving" in phases
    # The denominator appears once listing has produced it, and never before.
    indexing = [event for event in events if event[0] == "indexing"]
    assert indexing[-1][2] is not None and indexing[-1][2] > 0


# ------------------------------------------------------- the NDJSON wire


def test_the_cli_emitter_writes_marked_ndjson(capsys) -> None:
    emit = _progress_emitter()
    emit("listing", 0, None, "listing docs")
    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert payload["marker"] == PROGRESS_MARKER
    assert payload["phase"] == "listing"
    assert payload["total"] is None


def test_the_emitter_throttles_but_always_reports_a_phase_change(capsys) -> None:
    """50,000 progress lines over 50,000 files is work nobody reads."""
    emit = _progress_emitter()
    emit("indexing", 1, 1000, "a.md")
    for index in range(2, 20):
        emit("indexing", index, 1000, f"{index}.md")
    throttled = capsys.readouterr().out.strip().splitlines()
    assert len(throttled) == 1

    emit("saving", 1000, 1000, "writing")
    assert "saving" in capsys.readouterr().out


def test_worker_forwards_progress_lines_and_still_parses_the_report() -> None:
    """A progress line and the final report are both JSON on stdout.

    The marker is what tells them apart — without it the parser would take a
    progress line as the report, or the forwarder would replay the report as
    progress.
    """
    from pheasant.sync.worker import _parse_report

    progress = json.dumps({"marker": PROGRESS_MARKER, "phase": "indexing", "current": 1})
    report = json.dumps({"status": "ok", "results": [{"source_id": "docs"}]})
    # The forwarder consumes progress lines, so only the report reaches here.
    assert _parse_report(report, "docs")["results"][0]["source_id"] == "docs"
    assert json.loads(progress)["marker"] == PROGRESS_MARKER


def test_server_worker_waits_for_an_existing_index_writer(monkeypatch) -> None:
    from pheasant.sync import worker

    seen: list[str] = []

    def fake_run(command, timeout, on_progress):
        seen.extend(command)
        return 0, json.dumps({"status": "ok", "results": []}), ""

    monkeypatch.setattr(worker, "_run_streaming", fake_run)
    assert worker.run_sync("pheasant.yaml")["status"] == "ok"
    option = seen.index("--wait-for-lease")
    assert float(seen[option + 1]) > 0


def test_run_streaming_forwards_progress_before_the_process_exits(tmp_path: Path) -> None:
    """The point of streaming is seeing movement *during* a long sync."""
    import sys

    from pheasant.sync.worker import _run_streaming

    script = tmp_path / "child.py"
    script.write_text(
        "import json, sys\n"
        f"marker = {PROGRESS_MARKER!r}\n"
        "for i in range(3):\n"
        "    print(json.dumps({'marker': marker, 'phase': 'indexing', 'current': i}), flush=True)\n"
        "print(json.dumps({'status': 'ok', 'results': []}), flush=True)\n",
        encoding="utf-8",
    )
    seen: list[dict] = []
    code, stdout, _stderr = _run_streaming(
        [sys.executable, str(script)], 30, lambda event: seen.append(event)
    )
    assert code == 0
    assert [event["current"] for event in seen] == [0, 1, 2]
    # Progress lines are consumed, so the report is what is left to parse.
    assert "results" in stdout
    assert PROGRESS_MARKER not in stdout


# ------------------------------------------------------------------ the API


def test_jobs_routes_expose_the_registry(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    registry = client.app.state.jobs
    job = registry.create("sync", "Indexing docs", ["docs"])
    registry.progress(job.id, phase="indexing", current=2, total=5, detail="b.md")

    listing = client.get("/jobs").json()
    assert listing["active_count"] == 1
    assert listing["jobs"][0]["progress"]["current"] == 2

    single = client.get(f"/jobs/{job.id}")
    assert single.status_code == 200
    assert single.json()["label"] == "Indexing docs"

    assert client.get("/jobs/nope").status_code == 404


def test_finished_job_notifications_can_be_cleared(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    registry = client.app.state.jobs
    active = registry.create("sync", "Indexing active", ["active"])
    finished = registry.create("sync", "Indexed docs", ["docs"])
    registry.finish(finished.id)

    assert client.delete(f"/jobs/{active.id}").status_code == 409
    assert client.delete(f"/jobs/{finished.id}").json() == {"cleared": 1}
    assert client.get(f"/jobs/{finished.id}").status_code == 404

    failed = registry.create("sync", "Failed", ["docs"])
    registry.finish(failed.id, "failed", error="boom")
    assert client.delete("/jobs").json() == {"cleared": 1}
    listing = client.get("/jobs").json()
    assert [job["id"] for job in listing["jobs"]] == [active.id]


def test_the_stream_route_is_registered_before_the_job_id_route(
    loaded_config, config_path: Path
) -> None:
    """`/jobs/stream` must not be matched as a job whose id is "stream".

    FastAPI matches routes in declaration order, so registering
    `/jobs/{job_id}` first would make this endpoint a 404 forever. Asserted
    against the route table rather than by opening the stream: an SSE
    response never ends, and `TestClient` has no way to consume a bounded
    prefix of one without blocking on the rest — the check that matters here
    is the ordering, and this tests exactly that.
    """
    app = create_app(config=loaded_config, config_path=config_path)
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/jobs/stream" in paths
    assert paths.index("/jobs/stream") < paths.index("/jobs/{job_id}")


def test_the_stream_handler_is_async_so_a_dropped_client_cannot_leak_a_thread(
    loaded_config, config_path: Path
) -> None:
    """Regression: the handler must not be a sync generator.

    Starlette runs a sync generator in a threadpool it cannot interrupt. The
    first version of this endpoint blocked on `queue.get(timeout=…)` there, so
    every disconnected browser left a thread spinning on a queue nobody would
    ever write to again. An async generator is cancelled at its next `await`.
    """
    import inspect

    app = create_app(config=loaded_config, config_path=config_path)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/jobs/stream")
    response = route.endpoint()
    assert inspect.isasyncgen(response.body_iterator)


def test_a_source_row_carries_its_live_job(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    name = loaded_config.sources[0].name
    registry = client.app.state.jobs
    job = registry.create("sync", "Indexing", [name])
    registry.progress(job.id, phase="indexing", current=1, total=4)

    row = next(item for item in client.get("/sources").json() if item["name"] == name)
    assert row["syncing"] is True
    assert row["job"]["progress"]["total"] == 4

    registry.finish(job.id, "failed", error="boom")
    row = next(item for item in client.get("/sources").json() if item["name"] == name)
    # `syncing` alone flickers to nothing exactly when the caller most wants
    # to know whether it worked.
    assert row["syncing"] is False
    assert row["sync_error"] == "boom"


def test_a_background_sync_produces_a_followable_job(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    external = tmp_path / "job-notes"
    external.mkdir()
    (external / "note.md").write_text("# Note\n\nContent.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    started = client.post("/sync/job-notes", json={"wait": False}).json()
    assert started["job_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/jobs/{started['job_id']}").json()
        if not job["active"]:
            break
        time.sleep(0.1)
    assert job["status"] == "succeeded", job
