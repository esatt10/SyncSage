"""Integration tests for the live filesystem watcher (Synapse step 21.1)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from pheasant.config.loader import load_config
from pheasant.sync.engine import SyncEngine
from pheasant.sync.watcher import WatcherService

POLL_TIMEOUT_S = 5.0


def _wait_for(
    predicate: Callable[[], bool],
    timeout: float = POLL_TIMEOUT_S,
    interval: float = 0.05,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_watcher_reindexes_only_the_touched_artifact(
    config_path: Path,
    workspace_copy: Path,
) -> None:
    cfg = load_config(config_path)
    cfg.sync.watcher.debounce_ms = 200
    cfg.sync.watcher.batch_window_ms = 1000
    engine = SyncEngine(cfg)
    engine.sync_source("pheasant-repo", "full")
    before = {
        relative: dict(entry)
        for relative, entry in engine.manifests.load("pheasant-repo")["artifacts"].items()
    }
    assert "README.md" in before
    assert len(before) >= 2, "fixture repo should contain more than one artifact"

    baseline_threads = set(threading.enumerate())
    watcher = WatcherService(engine, cfg)
    watcher.start()
    assert watcher.running
    try:
        target = workspace_copy / "pheasant-repo" / "README.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n\nWatcher acceptance edit.\n",
            encoding="utf-8",
        )

        def touched_artifact_reindexed() -> bool:
            artifacts = engine.manifests.load("pheasant-repo")["artifacts"]
            entry = artifacts.get("README.md") or {}
            return bool(entry.get("sha256")) and entry["sha256"] != before["README.md"]["sha256"]

        # debounce (0.2 s) + 2 s budget per the step contract; poll generously.
        assert _wait_for(touched_artifact_reindexed, timeout=0.2 + 2.0), (
            "touched artifact was not re-indexed within debounce + 2 s"
        )
        after = engine.manifests.load("pheasant-repo")["artifacts"]
        assert set(after) == set(before)
        for relative, entry in after.items():
            if relative == "README.md":
                assert entry["sha256"] != before[relative]["sha256"]
                assert entry["last_indexed_at"] >= before[relative]["last_indexed_at"]
            else:
                assert entry == before[relative], f"untouched artifact re-indexed: {relative}"
        # Untouched sources were never synced by the watcher.
        assert not engine.manifests.exists("architecture-notes")
        assert not engine.manifests.exists("product-documents")
    finally:
        watcher.stop()
    assert not watcher.running
    assert _wait_for(lambda: not (set(threading.enumerate()) - baseline_threads)), (
        f"leaked threads: {set(threading.enumerate()) - baseline_threads}"
    )


def test_watcher_disabled_in_config_is_noop(config_path: Path) -> None:
    cfg = load_config(config_path)
    cfg.sync.watcher.enabled = False
    engine = SyncEngine(cfg)
    baseline_threads = set(threading.enumerate())
    watcher = WatcherService(engine, cfg)
    watcher.start()
    assert not watcher.running
    assert set(threading.enumerate()) == baseline_threads
    watcher.stop()


def test_watcher_without_filesystem_sources_is_noop(config_path: Path) -> None:
    cfg = load_config(config_path)
    cfg.sources = []
    engine = SyncEngine(cfg)
    baseline_threads = set(threading.enumerate())
    watcher = WatcherService(engine, cfg)
    watcher.start()
    assert not watcher.running
    assert set(threading.enumerate()) == baseline_threads
    watcher.stop()
