"""Indexing runs in a child process, so it cannot starve the server.

The GIL made this an architectural problem rather than a tuning one: while a
sync ran in the serving process, request threads barely got scheduled (a model
call measured 0.57s idle and 107s during a sync). The fix is to stop sharing
an interpreter — the server spawns `python -m syncsage sync` and adopts the
graph it produces.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncsage.config.loader import load_config
from syncsage.sync.engine import SyncEngine
from syncsage.sync.worker import WorkerBackedEngine, run_sync
from tests.conftest import CONFIG_TEMPLATE


def _render_config(tmp_path: Path, name: str, workspace: Path) -> Path:
    base = tmp_path / name
    for sub in ("state", "vault", "exports"):
        (base / sub).mkdir(parents=True)
    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace.as_posix(),
        state_path=(base / "state").as_posix(),
        vault_path=(base / "vault").as_posix(),
        exports_path=(base / "exports").as_posix(),
    )
    config_file = base / "syncsage.yaml"
    config_file.write_text(rendered, encoding="utf-8")
    return config_file


def test_worker_indexes_and_reports(tmp_path, workspace_copy) -> None:
    config_file = _render_config(tmp_path, "worker", workspace_copy)

    report = run_sync(config_file, "syncsage-repo", "full")

    assert report["status"] == "ok", report
    assert report["results"], "the worker reported no results"
    result = report["results"][0]
    assert result["source_id"] == "syncsage-repo"
    assert result["indexed_artifacts"] > 0
    assert result["graph_nodes"] > 0


def test_the_serving_process_adopts_what_the_worker_built(tmp_path, workspace_copy) -> None:
    """The parent never indexes, yet ends up serving the indexed graph."""

    config_file = _render_config(tmp_path, "adopt", workspace_copy)
    engine = SyncEngine(load_config(config_file))
    try:
        assert engine.graph_builder.graph.number_of_nodes() <= 1, "expected an empty graph"

        worker = WorkerBackedEngine(engine, config_file)
        result = worker.sync_source("syncsage-repo", "full")

        assert result.indexed_artifacts > 0
        # The parent's in-memory graph now holds what the child produced.
        assert engine.graph_builder.graph.number_of_nodes() > 1
        assert engine.graph_builder.graph.number_of_nodes() == result.graph_nodes

        # And the node index the child wrote is usable from here.
        assert engine.node_index.candidates(["sync"]), "worker did not leave a usable index"
    finally:
        engine.close()


def test_a_failing_worker_is_reported_not_raised(tmp_path, workspace_copy) -> None:
    """A broken sync must not take down the process that is serving queries."""

    config_file = _render_config(tmp_path, "broken", workspace_copy)
    report = run_sync(config_file, "no-such-source", "full")
    assert report["status"] in {"failed", "ok"}
    if report["status"] == "failed":
        assert report["error"], "a failure should say what went wrong"
    else:
        assert report["results"] == [] or all(
            item["indexed_artifacts"] == 0 for item in report["results"]
        )


def test_report_parsing_ignores_noise_on_stdout() -> None:
    """A stray log line must not turn a good sync into a failed one."""

    from syncsage.sync.worker import _parse_report

    payload = {"status": "ok", "results": [{"source_id": "x", "indexed_artifacts": 3}]}
    stdout = "loading config\nsome warning\n" + json.dumps(payload) + "\n"
    assert _parse_report(stdout, "x") == payload

    assert _parse_report("no json here", "x")["status"] == "unknown"
    assert _parse_report("", None)["results"] == []
