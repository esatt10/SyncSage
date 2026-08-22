"""Acceptance tests for Phase 35.1 — metrics and per-source progress.

Two questions drive all of this, and neither could be answered before:

1. "Is the indexer moving, or is it hung?" — needs throughput, an ETA, and a
   stall signal that distinguishes *slow* from *stuck*.
2. "What should the autoscaler scale on?" — needs a real ``/metrics``.

The tests are written against the observable surfaces (the exposition text, the
job payload, the HTTP routes) rather than against internals, because those are
what a scraper and a UI actually consume.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.jobs import STALL_AFTER_SECONDS, JobRegistry
from pheasant.sync.engine import SyncEngine
from pheasant.telemetry import metrics

# ---------------------------------------------------------------------------
# Exposition format
# ---------------------------------------------------------------------------


def _parse_exposition(text: str) -> dict[str, float]:
    """A deliberately strict mini-parser.

    Asserting on substrings would pass for output no scraper accepts. This
    fails on anything that is not ``name{labels} value``, which is the actual
    contract with Prometheus.
    """

    samples: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        series, _, raw_value = line.rpartition(" ")
        assert series, f"unparseable exposition line: {line!r}"
        value = float("inf") if raw_value == "+Inf" else float(raw_value)
        samples[series] = value
    return samples


def test_exposition_text_is_wellformed_and_types_are_declared() -> None:
    registry = metrics.MetricsRegistry()
    registry.counter("demo_total", "A counter.", ("kind",))
    registry.gauge("demo_gauge", "A gauge.")
    registry.inc("demo_total", 2, kind="a")
    registry.inc("demo_total", 3, kind="a")
    registry.set("demo_gauge", 7.5)

    text = registry.render()
    assert "# TYPE demo_total counter" in text
    assert "# TYPE demo_gauge gauge" in text
    samples = _parse_exposition(text)
    assert samples['demo_total{kind="a"}'] == 5
    assert samples["demo_gauge"] == 7.5


def test_histogram_buckets_are_cumulative_and_agree_with_count() -> None:
    """The failure this pins is silent: a non-cumulative histogram still looks
    plausible in a curl, and every quantile computed from it is wrong."""

    registry = metrics.MetricsRegistry()
    registry.histogram("demo_seconds", "Latency.", buckets=(0.1, 1.0, 10.0))
    for value in (0.05, 0.5, 0.5, 5.0):
        registry.observe("demo_seconds", value)

    samples = _parse_exposition(registry.render())
    assert samples['demo_seconds_bucket{le="0.1"}'] == 1
    assert samples['demo_seconds_bucket{le="1"}'] == 3
    assert samples['demo_seconds_bucket{le="10"}'] == 4
    assert samples['demo_seconds_bucket{le="+Inf"}'] == 4
    assert samples["demo_seconds_count"] == 4
    assert samples["demo_seconds_sum"] == 6.05

    # Monotonic non-decreasing across bounds is the invariant a scraper relies
    # on; check it directly rather than trusting the four numbers above.
    ordered = [
        samples['demo_seconds_bucket{le="0.1"}'],
        samples['demo_seconds_bucket{le="1"}'],
        samples['demo_seconds_bucket{le="10"}'],
        samples['demo_seconds_bucket{le="+Inf"}'],
    ]
    assert ordered == sorted(ordered)


def test_label_values_are_escaped_so_one_bad_value_cannot_break_a_scrape() -> None:
    registry = metrics.MetricsRegistry()
    registry.gauge("demo_gauge", "A gauge.", ("source",))
    registry.set("demo_gauge", 1.0, source='we"ird\\path\nnewline')

    line = next(ln for ln in registry.render().splitlines() if ln.startswith("demo_gauge{"))
    assert '\\"' in line and "\\\\" in line and "\\n" in line
    # A raw newline would split one sample into two unparseable lines.
    assert len(line.splitlines()) == 1
    _parse_exposition(registry.render())


def test_invalid_metric_names_are_rejected_at_registration() -> None:
    registry = metrics.MetricsRegistry()
    for bad in ("has-a-dash", "1leading_digit", "has space"):
        try:
            registry.gauge(bad, "nope")
        except ValueError:
            continue
        raise AssertionError(f"registered an invalid metric name: {bad!r}")


def test_registration_is_idempotent_and_never_clears_recorded_values() -> None:
    """``create_app`` runs per process *and* per TestClient; a second call must
    not blank the counters the first one's traffic produced."""

    metrics.register_default_metrics("9.9.9")
    metrics.REGISTRY.inc("pheasant_search_total", 4, mode="hybrid", outcome="ok")
    metrics.register_default_metrics("9.9.9")
    assert metrics.REGISTRY.value("pheasant_search_total", mode="hybrid", outcome="ok") == 4


# ---------------------------------------------------------------------------
# Agent memory (Phase 0-5) — nothing here existed before that work, so a
# compaction change would otherwise be unfalsifiable at the metrics layer.
# ---------------------------------------------------------------------------


def test_memory_metric_surface_is_declared() -> None:
    # A private registry, not the process-wide `metrics.REGISTRY` — this
    # module's other tests accumulate on the shared one by design (see
    # `test_registration_is_idempotent_...` above), so asserting an exact
    # value against it here would be order-dependent on whatever else in
    # this file already ran.
    registry = metrics.MetricsRegistry()
    saved = metrics.REGISTRY
    try:
        metrics.REGISTRY = registry
        metrics.register_default_metrics("9.9.9")
        registry.set("pheasant_memory_records", 3, scope="org", tier="hot")
        registry.inc("pheasant_memory_writes_total", outcome="reinforced")
        registry.inc("pheasant_memory_l0_folds_total", kind="normalized")
        registry.observe("pheasant_memory_maintenance_seconds", 0.01)
        registry.inc("pheasant_memory_compactions_total", 2, op="subsume")
        registry.observe("pheasant_memory_compaction_seconds", 0.02)
        registry.inc("pheasant_memory_synthesis_calls_total", outcome="synthesized")
        registry.set("pheasant_memory_reinforcement_ratio", 0.5)

        samples = _parse_exposition(registry.render())
        assert samples['pheasant_memory_records{scope="org",tier="hot"}'] == 3
        assert samples['pheasant_memory_writes_total{outcome="reinforced"}'] == 1
        assert samples['pheasant_memory_l0_folds_total{kind="normalized"}'] == 1
        assert samples["pheasant_memory_maintenance_seconds_count"] == 1
        assert samples['pheasant_memory_compactions_total{op="subsume"}'] == 2
        assert samples["pheasant_memory_compaction_seconds_count"] == 1
        assert samples['pheasant_memory_synthesis_calls_total{outcome="synthesized"}'] == 1
        assert samples["pheasant_memory_reinforcement_ratio"] == 0.5
    finally:
        metrics.REGISTRY = saved


def test_reinforcement_ratio_counts_paraphrases_not_verbatim_repeats() -> None:
    """The distinction the gauge exists for. With `reinforcement_enabled` on
    — the default — a byte-identical re-write also reports
    `outcome="reinforced"`, so a ratio derived from `outcome` alone would
    credit reinforcement for the exact dedup that predates it and read high
    on a store that only ever sees verbatim repeats."""

    registry = metrics.MetricsRegistry()
    saved = metrics.REGISTRY
    try:
        metrics.REGISTRY = registry
        metrics.register_default_metrics("9.9.9")

        # Four verbatim repeats of one record: reinforcement is doing nothing
        # the pre-Phase-1 dedup would not have done for free.
        metrics.record_memory_write("created", None)
        for _ in range(4):
            metrics.record_memory_write("reinforced", "exact")
        assert registry.value("pheasant_memory_reinforcement_ratio") == 0.0

        # One paraphrase folded: now it is earning its keep.
        metrics.record_memory_write("reinforced", "normalized")
        assert registry.value("pheasant_memory_reinforcement_ratio") == 0.5

        metrics.record_memory_write("created", None)
        metrics.record_memory_write("created", None)
        assert registry.value("pheasant_memory_reinforcement_ratio") == 0.25
    finally:
        metrics.REGISTRY = saved


def test_reinforcement_ratio_is_zero_before_any_write() -> None:
    registry = metrics.MetricsRegistry()
    saved = metrics.REGISTRY
    try:
        metrics.REGISTRY = registry
        metrics.register_default_metrics("9.9.9")
        metrics.record_memory_write("duplicate", "exact")
        assert registry.value("pheasant_memory_reinforcement_ratio") == 0.0
    finally:
        metrics.REGISTRY = saved


def test_the_ratio_reflects_what_the_real_write_path_produces(tmp_path: Path) -> None:
    """The end-to-end version, and the one that would have caught the gauge
    being wrong: drive real `memory_write` calls rather than hand-incremented
    counters, so the test cannot pass against a state the default config
    never produces."""

    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    (tmp_path / "memory").mkdir()
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: ratio
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )

    registry = metrics.MetricsRegistry()
    saved = metrics.REGISTRY
    tools = PheasantTools(load_config(config_path))
    try:
        metrics.REGISTRY = registry
        metrics.register_default_metrics("9.9.9")

        tools.memory_write("ratio", "The paydb service runs in us-east-2.", scope="org")
        # Verbatim repeat: reports `reinforced` (its counters really do move)
        # but must not move the ratio.
        assert (
            tools.memory_write("ratio", "The paydb service runs in us-east-2.", scope="org")[
                "outcome"
            ]
            == "reinforced"
        )
        assert registry.value("pheasant_memory_reinforcement_ratio") == 0.0

        # A paraphrase of the same claim: this is the redundancy L0 removes.
        assert (
            tools.memory_write(
                "ratio", "Note that the PAYDB service runs in us-east-2!", scope="org"
            )["outcome"]
            == "reinforced"
        )
        assert registry.value("pheasant_memory_reinforcement_ratio") == 0.5
        assert registry.value("pheasant_memory_l0_folds_total", kind="exact") == 1
        assert registry.value("pheasant_memory_l0_folds_total", kind="normalized") == 1
    finally:
        metrics.REGISTRY = saved
        tools.engine.close()


# ---------------------------------------------------------------------------
# Per-source progress
# ---------------------------------------------------------------------------


def test_two_sources_in_one_job_keep_separate_counters() -> None:
    """The pre-35.1 shape: one job, one counter, so a stuck source was hidden
    behind the ones that were fine."""

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing all sources", ["docs", "code"])

    jobs.progress(job.id, phase="preparing", current=5, total=10, source="docs")
    jobs.progress(job.id, phase="preparing", current=1, total=900, source="code")

    payload = jobs.get(job.id)
    by_source = {row["source"]: row for row in payload["sources"]}
    assert set(by_source) == {"docs", "code"}
    assert (by_source["docs"]["current"], by_source["docs"]["total"]) == (5, 10)
    assert (by_source["code"]["current"], by_source["code"]["total"]) == (1, 900)
    assert by_source["docs"]["fraction"] == 0.5
    assert by_source["code"]["fraction"] < 0.01

    # The rollup still exists for pre-35.1 callers, and sums honestly.
    assert payload["progress"]["current"] == 6
    assert payload["progress"]["total"] == 910


def test_rollup_total_stays_none_until_every_source_knows_its_own() -> None:
    """A partial sum shrinks the denominator as sources report in, which runs
    the progress bar *backwards* — worse than showing no bar at all."""

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing all sources", ["docs", "code"])
    jobs.progress(job.id, phase="preparing", current=5, total=10, source="docs")
    jobs.progress(job.id, phase="listing", current=0, source="code")

    assert jobs.get(job.id)["progress"]["total"] is None

    jobs.progress(job.id, phase="preparing", current=0, total=40, source="code")
    assert jobs.get(job.id)["progress"]["total"] == 50


def test_throughput_and_eta_are_derived_from_observed_updates() -> None:
    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["docs"])
    jobs.progress(job.id, phase="preparing", current=0, total=100, source="docs")
    for step in range(1, 6):
        time.sleep(0.02)
        jobs.progress(job.id, phase="preparing", current=step * 10, total=100, source="docs")

    record = jobs.get(job.id)["sources"][0]
    assert record["files_per_second"] is not None and record["files_per_second"] > 0
    assert record["eta_seconds"] is not None
    # 50 of 100 done at the observed rate — the ETA should be in the same
    # ballpark as the time already spent, not orders of magnitude off.
    assert 0 < record["eta_seconds"] < 60


def test_eta_is_none_while_the_denominator_is_unknown() -> None:
    """No total means no honest ETA. Inventing one is how a bar lies."""

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["docs"])
    jobs.progress(job.id, phase="listing", current=1, source="docs")
    time.sleep(0.02)
    jobs.progress(job.id, phase="listing", current=9, source="docs")

    record = jobs.get(job.id)["sources"][0]
    assert record["total"] is None
    assert record["eta_seconds"] is None
    assert record["fraction"] is None


def test_slow_is_not_stuck_but_silent_eventually_is(monkeypatch) -> None:
    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["docs"])
    jobs.progress(job.id, phase="preparing", current=1, total=10, source="docs")

    record = jobs.get(job.id)["sources"][0]
    assert record["stalled"] is False
    assert record["seconds_since_progress"] < 5

    # Jump the clock past the stall window rather than sleeping through it.
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + STALL_AFTER_SECONDS + 10)
    stalled = jobs.get(job.id)["sources"][0]
    assert stalled["stalled"] is True
    assert stalled["seconds_since_progress"] > STALL_AFTER_SECONDS


def test_a_finished_source_is_never_reported_stalled(monkeypatch) -> None:
    """Otherwise every completed job in the history turns red once it ages."""

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["docs"])
    jobs.progress(job.id, phase="preparing", current=10, total=10, source="docs")
    jobs.finish(job.id, "succeeded")

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + STALL_AFTER_SECONDS * 10)
    payload = jobs.get(job.id)
    assert payload["sources"][0]["stalled"] is False
    assert payload["stalled"] is False


def test_phase_change_clears_a_stale_denominator_and_rate_window() -> None:
    """Counters are phase-local — files while preparing, chunks while
    embedding. Carrying 900/1000 into a new phase produces a plausible,
    entirely false bar, and mixes units in the rate window."""

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["docs"])
    jobs.progress(job.id, phase="preparing", current=900, total=1000, source="docs")
    jobs.progress(job.id, phase="embedding", current=3, source="docs")

    record = jobs.get(job.id)["sources"][0]
    assert record["total"] is None
    assert record["fraction"] is None
    assert record["files_per_second"] is None


def test_finishing_one_source_leaves_the_rest_running() -> None:
    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing all sources", ["docs", "code"])
    jobs.progress(job.id, phase="preparing", current=10, total=10, source="docs")
    jobs.finish_source(job.id, "docs", "succeeded")

    by_source = {row["source"]: row for row in jobs.get(job.id)["sources"]}
    assert by_source["docs"]["active"] is False
    assert by_source["code"]["active"] is True
    assert jobs.get(job.id)["active"] is True


def test_seeded_targets_show_as_queued_before_anything_reports() -> None:
    """'Waiting its turn' and 'missing entirely' are different answers."""

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing all sources", ["docs", "code"])
    rows = {row["source"]: row for row in jobs.get(job.id)["sources"]}
    assert set(rows) == {"docs", "code"}
    assert all(row["phase"] == "queued" for row in rows.values())


def test_metrics_sample_reports_queue_depth_and_drops_finished_work() -> None:
    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing all sources", ["docs", "code"])
    jobs.progress(job.id, phase="preparing", current=1, total=10, source="docs")

    sample = jobs.metrics_sample()
    assert sample["pheasant_index_inflight"] == 1
    assert sample["pheasant_index_queue_depth"] == 2
    assert sample["pheasant_index_progress_ratio"]["docs"] == 0.1

    jobs.finish(job.id, "succeeded")
    done = jobs.metrics_sample()
    assert done["pheasant_index_inflight"] == 0
    assert done["pheasant_index_queue_depth"] == 0


def test_publishing_a_snapshot_is_safe_while_the_job_is_being_written() -> None:
    """`snapshot = job` is a reference, not a copy.

    `_publish` serialized it outside the lock, so `as_dict` walked
    `job.sources` and `job.log` while an indexing thread was adding to them.
    That is a torn payload at best and `RuntimeError: dictionary changed size
    during iteration` at worst — raised from inside a progress callback, on the
    sync path, which is the one place `progress()` documents it must never
    fail.
    """

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["src-0"])
    errors: list[BaseException] = []
    stop = threading.Event()

    def write(offset: int) -> None:
        try:
            for index in range(400):
                jobs.progress(
                    job.id,
                    phase="preparing",
                    current=index,
                    total=400,
                    detail=f"file-{index}",
                    source=f"src-{offset}-{index % 40}",
                )
        except BaseException as exc:  # noqa: BLE001 - the point is that none escape
            errors.append(exc)
        finally:
            stop.set()

    def read() -> None:
        try:
            while not stop.is_set():
                jobs.list()
                jobs.get(job.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(3)]
    threads.append(threading.Thread(target=read))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"a concurrent publish raised: {errors[:3]}"


def test_a_finished_sources_gauge_stops_being_exported() -> None:
    """A live gauge must not freeze at its last reading.

    `render_with` merged each scrape's per-source values into whatever the
    registry already held, so a source that finished — or was deleted — kept
    exporting its final progress ratio forever, as a *current* reading.
    Prometheus cannot distinguish that from a source genuinely stuck at 10%,
    and the label set grows without bound as sources come and go.
    """

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing", ["docs"])
    jobs.progress(job.id, phase="preparing", current=1, total=10, source="docs")

    first = metrics.render_with(jobs.metrics_sample())
    assert 'pheasant_index_progress_ratio{source="docs"}' in first

    jobs.finish(job.id, "succeeded")
    after = metrics.render_with(jobs.metrics_sample())
    assert 'pheasant_index_progress_ratio{source="docs"}' not in after, (
        "a finished source is still exporting its last progress ratio"
    )


# ---------------------------------------------------------------------------
# The wire: engine -> hook
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path, name: str, files: int) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    for index in range(files):
        (root / f"note-{index}.md").write_text(
            f"# Note {index}\n\nDeterministic body about gateways and retries.\n",
            encoding="utf-8",
        )
    return root


def _config(tmp_path: Path, sources: list[dict[str, Any]]) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "observability",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
            "sources": sources,
        }
    )


def test_engine_tags_every_progress_event_with_its_source_and_counters(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "docs", 4)
    config = _config(
        tmp_path,
        [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    )
    engine = SyncEngine(config)
    events: list[dict[str, Any]] = []

    def hook(phase, current, total, detail, meta=None):
        events.append({"phase": phase, "current": current, "meta": meta or {}})

    try:
        engine.sync_source("docs", "full", on_progress=hook)
    finally:
        engine.close()

    assert events, "the engine emitted no progress at all"
    assert {event["meta"].get("source") for event in events} == {"docs"}
    committing = [e for e in events if e["phase"] == "committing"]
    assert committing, "no commit-phase events"
    last = committing[-1]["meta"]
    assert last["indexed"] == 4
    assert last["bytes_done"] > 0


def test_a_four_argument_hook_still_works(tmp_path: Path) -> None:
    """``on_progress`` is a public parameter. An existing caller passing the
    pre-35.1 four-argument callback must not start raising ``TypeError``."""

    workspace = _workspace(tmp_path, "docs", 2)
    config = _config(
        tmp_path,
        [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    )
    engine = SyncEngine(config)
    seen: list[str] = []

    def legacy_hook(phase, current, total, detail):
        seen.append(phase)

    try:
        result = engine.sync_source("docs", "full", on_progress=legacy_hook)
    finally:
        engine.close()

    assert result.indexed_artifacts == 2
    assert "committing" in seen


def test_sync_all_reports_each_source_separately(tmp_path: Path) -> None:
    docs = _workspace(tmp_path, "docs", 3)
    code = _workspace(tmp_path, "code", 2)
    config = _config(
        tmp_path,
        [
            {"name": "docs", "type": "markdown_folder", "path": str(docs), "include": ["**/*.md"]},
            {"name": "code", "type": "markdown_folder", "path": str(code), "include": ["**/*.md"]},
        ],
    )
    engine = SyncEngine(config)
    per_source: dict[str, int] = {}

    def hook(phase, current, total, detail, meta=None):
        name = (meta or {}).get("source")
        if name and phase == "committing":
            per_source[name] = max(per_source.get(name, 0), (meta or {}).get("indexed", 0))

    try:
        engine.sync_all("full", on_progress=hook)
    finally:
        engine.close()

    assert per_source == {"docs": 3, "code": 2}


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_metrics_endpoint_serves_prometheus_text_not_json(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "docs", 2)
    config = _config(
        tmp_path,
        [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    )
    client = TestClient(create_app(config=config))

    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    samples = _parse_exposition(response.text)
    assert samples["pheasant_up"] == 1
    # The stub this replaced returned only `pheasant_up 1`; the point of the
    # step is that there is now something to scale on.
    for expected in (
        "pheasant_index_queue_depth",
        "pheasant_index_inflight",
        "pheasant_graph_nodes",
        "pheasant_process_resident_bytes",
    ):
        assert expected in samples, f"{expected} missing from /metrics"
    assert any(key.startswith("pheasant_build_info{") for key in samples)


def test_search_latency_is_recorded_per_mode(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "docs", 2)
    config = _config(
        tmp_path,
        [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    )
    client = TestClient(create_app(config=config))
    client.post("/sync/docs", json={"mode": "full", "wait": True})

    before = metrics.REGISTRY.value("pheasant_search_total", mode="text", outcome="ok") or 0
    client.post("/search", json={"query": "gateways", "mode": "text", "max_results": 3})
    after = metrics.REGISTRY.value("pheasant_search_total", mode="text", outcome="ok") or 0
    assert after == before + 1

    samples = _parse_exposition(client.get("/metrics").text)
    assert samples['pheasant_search_duration_seconds_count{mode="text"}'] >= 1


def test_sources_route_carries_this_sources_slice_of_a_running_job(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "docs", 2)
    config = _config(
        tmp_path,
        [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    )
    app = create_app(config=config)
    client = TestClient(app)

    jobs = app.state.jobs
    job = jobs.create("sync", "Indexing docs", ["docs"])
    jobs.progress(job.id, phase="preparing", current=1, total=2, source="docs")

    rows = client.get("/sources").json()
    record = next(row for row in rows if row["name"] == "docs")
    assert record["syncing"] is True
    assert record["progress"]["source"] == "docs"
    assert record["progress"]["phase"] == "preparing"
    assert record["progress"]["fraction"] == 0.5


def test_an_update_without_a_source_still_lands_on_a_single_target_job() -> None:
    """Back-compat for the pre-35.1 wire.

    A four-argument hook sends no ``meta``, so ``source`` arrives as None. A
    job with exactly one target has an unambiguous answer and must use it —
    otherwise every legacy caller's progress silently accumulates under a
    placeholder record and the source's own row stays empty at "queued".

    Found by mutation testing: every other test in this file passes ``source``
    explicitly, so neutering this fallback changed nothing and the whole
    branch was uncovered.
    """

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing docs", ["docs"])
    jobs.progress(job.id, phase="preparing", current=3, total=4, detail="a.md")

    rows = {row["source"]: row for row in jobs.get(job.id)["sources"]}
    assert set(rows) == {"docs"}, "a placeholder record was created instead of using the target"
    assert rows["docs"]["current"] == 3
    assert rows["docs"]["phase"] == "preparing"


def test_an_ambiguous_sourceless_update_does_not_corrupt_a_real_source() -> None:
    """With several targets there is no honest answer, so it must not guess.

    Attributing an unlabelled update to (say) the alphabetically-first source
    would make one source's bar jump on another's work — a wrong number is
    worse than a clearly-unattributed one.
    """

    jobs = JobRegistry()
    job = jobs.create("sync", "Indexing all sources", ["docs", "code"])
    jobs.progress(job.id, phase="preparing", current=7, total=9)

    rows = {row["source"]: row for row in jobs.get(job.id)["sources"]}
    assert rows["docs"]["current"] == 0
    assert rows["code"]["current"] == 0
    assert rows["*"]["current"] == 7
