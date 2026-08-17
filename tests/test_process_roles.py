"""Phase 35.6a — process roles.

The property under test is narrow: `all` is unchanged, and no other role
indexes when it should not. Everything else — the startup refusal, the 409,
the drain loop — exists to make that safe to rely on.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.deployment.roles import (
    POLICIES,
    Role,
    RoleConfigurationError,
    describe,
    resolve_role,
    validate_role,
)
from pheasant.sync.queue import DONE, PENDING, LocalQueue


def _config(
    tmp_path: Path,
    *,
    state_name: str = "state",
    sources: int = 2,
    queue: bool = False,
    role: str | None = None,
) -> PheasantConfig:
    workspace = tmp_path / f"workspace-{state_name}"
    entries = []
    for index in range(sources):
        folder = workspace / f"src{index}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "note.md").write_text(
            f"# Note {index}\n\nThe deployment gateway rotates credentials nightly.\n",
            encoding="utf-8",
        )
        entries.append(
            {
                "name": f"src{index}",
                "type": "markdown_folder",
                "path": str(folder),
                "include": ["**/*.md"],
            }
        )
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "roles",
            "state_path": str(tmp_path / state_name),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / f"{state_name}-exports"),
        },
        "storage": {"graph_snapshots": False},
        "sync": {"concurrency": {"lock_timeout_seconds": 5}},
        "sources": entries,
    }
    if queue:
        payload["sync"]["queue"] = {"enabled": True}
    if role is not None:
        payload["server"] = {"role": role}
    return PheasantConfig.model_validate(payload)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_the_default_role_is_all_and_is_todays_behavior(tmp_path: Path) -> None:
    policy = resolve_role(_config(tmp_path))

    assert policy.role is Role.ALL
    assert policy.is_default
    assert policy.runs_watcher and policy.runs_scheduler and policy.indexes_locally
    # `all` must NOT drain, or switching the queue on for crash resumption
    # would quietly turn a single container into a fleet member.
    assert policy.drains_queue is False


def test_the_flag_beats_the_config(tmp_path: Path) -> None:
    config = _config(tmp_path, role="indexer")
    assert resolve_role(config).role is Role.INDEXER
    assert resolve_role(config, "worker").role is Role.WORKER


def test_an_unknown_role_raises_rather_than_defaulting(tmp_path: Path) -> None:
    """A typo in a Deployment's args must not silently produce a full pod."""

    with pytest.raises(RoleConfigurationError, match="Unknown role"):
        resolve_role(_config(tmp_path), "indexr")


@pytest.mark.parametrize("role", list(Role))
def test_every_role_has_a_policy_and_describes_itself(role: Role) -> None:
    policy = POLICIES[role]
    described = describe(policy)
    assert described["role"] == role.value
    assert set(described) == {
        "role",
        "watcher",
        "scheduler",
        "drains_queue",
        "indexes_locally",
        "refreshes_graph",
    }


def test_only_the_indexer_drains_and_only_serving_roles_index() -> None:
    """The whole table, asserted rather than left to the docstring."""

    assert [role.value for role in Role if POLICIES[role].drains_queue] == ["indexer"]
    assert sorted(role.value for role in Role if POLICIES[role].indexes_locally) == [
        "all",
        "indexer",
    ]
    assert sorted(role.value for role in Role if POLICIES[role].runs_watcher) == ["all", "indexer"]


# --------------------------------------------------------------------------
# The startup refusal
# --------------------------------------------------------------------------


def test_the_api_role_refuses_to_start_without_a_queue(tmp_path: Path) -> None:
    """Otherwise every sync request is accepted and never runs."""

    config = _config(tmp_path, role="api")
    with pytest.raises(RoleConfigurationError, match="sync.queue.enabled"):
        validate_role(resolve_role(config), config)

    with pytest.raises(RoleConfigurationError):
        create_app(config, config_path=str(tmp_path / "pheasant.yaml"), role="api")


def test_the_api_role_starts_with_a_queue(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="api-ok", role="api", queue=True)
    validate_role(resolve_role(config), config)
    client = TestClient(create_app(config, role="api"))
    assert client.get("/health").json()["role"] == "api"


@pytest.mark.parametrize("role", ["all", "indexer", "worker"])
def test_other_roles_need_no_queue(tmp_path: Path, role: str) -> None:
    """Only `api` depends on a queue: the rest can index or do nothing."""

    config = _config(tmp_path, state_name=f"noqueue-{role}", role=role)
    validate_role(resolve_role(config), config)


# --------------------------------------------------------------------------
# What the role changes at runtime
# --------------------------------------------------------------------------


def test_health_and_ready_identify_the_pod(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="probes", role="indexer")
    client = TestClient(create_app(config, role="indexer"))

    health = client.get("/health").json()
    assert health["status"] == "ok" and health["role"] == "indexer"

    ready = client.get("/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["role"] == "indexer"
    assert body["drains_queue"] is True
    assert body["knowledge_base"] == config.knowledge_base_id


def test_ready_reports_503_when_the_state_store_is_unreachable(tmp_path: Path) -> None:
    """Readiness takes a pod out of the Service; liveness restarts it.

    A state store that has gone away is the first case, not the second, so
    `/ready` must actually check something rather than return a constant.
    """

    config = _config(tmp_path, state_name="unready")
    app = create_app(config)
    client = TestClient(app)
    assert client.get("/ready").status_code == 200

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is gone")

    app.state.state.rows = broken  # type: ignore[method-assign]
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    # Liveness must stay green: restarting the pod does not bring a database
    # back, and a restart loop is worse than a pod that stops taking traffic.
    assert client.get("/health").status_code == 200


def test_the_api_role_publishes_a_sync_instead_of_running_it(tmp_path: Path) -> None:
    """The load-bearing cell of the table.

    Three api replicas that each indexed on request would put three processes
    on one source. Publishing is what makes the replica count free.
    """

    config = _config(tmp_path, state_name="api-publish", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    response = client.post("/sync/src0", json={"mode": "full", "wait": False})
    assert response.status_code == 200
    assert response.json()["status"] == "syncing"

    engine = client.app.state.engine
    queue = LocalQueue(engine.state)
    assert queue.depth()[PENDING] == 1
    # Nothing was indexed here: the artifacts table is still empty.
    rows = engine.state.rows("SELECT COUNT(*) AS c FROM artifacts", ())
    assert int(rows[0]["c"]) == 0


def test_the_api_role_deduplicates_a_double_click(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="api-dedup", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    first = client.post("/sync/src0", json={"mode": "full", "wait": False}).json()
    second = client.post("/sync/src0", json={"mode": "full", "wait": False}).json()

    assert first["job_id"] == second["job_id"]
    assert LocalQueue(client.app.state.engine.state).depth()[PENDING] == 1


def test_the_api_role_refuses_a_blocking_sync_with_a_usable_message(tmp_path: Path) -> None:
    """409 and the fix, rather than lying or quietly changing the contract."""

    config = _config(tmp_path, state_name="api-block", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    response = client.post("/sync/src0", json={"mode": "full", "wait": True})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "role 'api' does not index" in detail
    assert "wait=false" in detail


def test_the_all_role_still_indexes_in_process(tmp_path: Path) -> None:
    """Rule 7 in role form: the default path is untouched."""

    config = _config(tmp_path, state_name="all-index")
    client = TestClient(create_app(config))

    response = client.post("/sync/src0", json={"mode": "full", "wait": True})
    assert response.status_code == 200
    assert response.json()["indexed_artifacts"] == 1

    rows = client.app.state.engine.state.rows("SELECT COUNT(*) AS c FROM index_tasks", ())
    assert int(rows[0]["c"]) == 0, "the default role published instead of indexing"


# --------------------------------------------------------------------------
# The indexer's drain loop
# --------------------------------------------------------------------------


def _write_config_file(config: PheasantConfig, path: Path, *, role: str) -> Path:
    # Written literally: the repo-root `yaml.py` shim shadows PyYAML from a
    # checkout and does not round-trip a dump of this shape.
    lines = [
        "pheasant:",
        f"  name: {config.pheasant.name}",
        f"  state_path: {config.pheasant.state_path}",
        f"  workspace_root: {config.pheasant.workspace_root}",
        f"  exports_path: {config.pheasant.exports_path}",
        "storage:",
        "  graph_snapshots: false",
        "server:",
        f"  role: {role}",
        "sync:",
        "  queue:",
        "    enabled: true",
        "sources:",
    ]
    for source in config.sources:
        lines += [
            f"  - name: {source.name}",
            "    type: markdown_folder",
            f"    path: {source.path}",
            "    include:",
            '      - "**/*.md"',
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_indexer_drains_what_an_api_replica_published(tmp_path: Path) -> None:
    """The hand-off, end to end, across two processes' worth of objects.

    An api replica publishes and indexes nothing; an indexer claims the task
    and the artifacts appear. This is the whole point of the role split, so it
    is tested as one flow rather than as two halves.
    """

    from pheasant.cli import _QueueDrainer

    config = _config(tmp_path, state_name="handoff", role="api", queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")

    api = TestClient(create_app(config, role="api"))
    api.post("/sync/src0", json={"mode": "full", "wait": False})
    api.post("/sync/src1", json={"mode": "full", "wait": False})
    assert LocalQueue(api.app.state.engine.state).depth()[PENDING] == 2

    from pheasant.config.loader import load_config

    indexer_config = load_config(config_path)
    drainer = _QueueDrainer(indexer_config, str(config_path))
    drainer.start()
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            depth = LocalQueue(api.app.state.engine.state).depth()
            if depth[DONE] == 2:
                break
            time.sleep(0.5)
    finally:
        drainer.stop()

    depth = LocalQueue(api.app.state.engine.state).depth()
    assert depth[DONE] == 2, f"indexer did not drain the backlog: {depth}"
    rows = api.app.state.engine.state.rows(
        "SELECT source_id, COUNT(*) AS c FROM artifacts GROUP BY source_id ORDER BY source_id"
    )
    assert [(row["source_id"], int(row["c"])) for row in rows] == [("src0", 1), ("src1", 1)]


def test_a_drainer_stops_promptly(tmp_path: Path) -> None:
    """SIGTERM must not wait out an idle poll, let alone a multi-hour index."""

    from pheasant.cli import _QueueDrainer

    config = _config(tmp_path, state_name="stop", sources=1, queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")

    from pheasant.config.loader import load_config

    drainer = _QueueDrainer(load_config(config_path), str(config_path))
    drainer.start()
    time.sleep(1.0)
    started = time.monotonic()
    drainer.stop()
    assert time.monotonic() - started < 10, "stop() did not return promptly"


def test_no_drainer_is_built_for_a_role_that_does_not_drain(tmp_path: Path) -> None:
    from pheasant.cli import _queue_drainer

    config = _config(tmp_path, state_name="nodrain", queue=True)
    for role in ("all", "api", "worker"):
        assert _queue_drainer(config, "pheasant.yaml", resolve_role(config, role)) is None
    assert _queue_drainer(config, "pheasant.yaml", resolve_role(config, "indexer")) is not None


# --------------------------------------------------------------------------
# The api role's graph refresh
# --------------------------------------------------------------------------


def test_only_the_api_role_refreshes_the_graph() -> None:
    """The other roles index their own graph and reload through the worker."""

    assert [role.value for role in Role if POLICIES[role].refreshes_graph] == ["api"]


def test_an_api_replica_picks_up_a_graph_another_process_wrote(tmp_path: Path) -> None:
    """The gap that made a role split quietly wrong.

    A process loads the graph once at startup, and the only reload path runs
    after a sync *that process* performed. An api replica never indexes, so
    without this it would answer graph queries from whatever the graph was
    when its pod started — indefinitely, and silently, while text and vector
    search stayed current from the shared database.
    """

    from pheasant.cli import _GraphRefresher
    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path, state_name="refresh", sources=1, role="api", queue=True)

    # The "indexer": a separate engine over the same /state that really syncs.
    indexer = SyncEngine(config)
    try:
        indexer.sync_source("src0", "full")
    finally:
        indexer.close()

    # The "api replica": a second engine, started before more content exists.
    api = SyncEngine(config)
    try:
        refresher = _GraphRefresher(api, interval_seconds=1)
        before = api.graph_builder.graph.number_of_nodes()
        assert before > 0
        assert refresher.check_once() is False, "nothing changed, yet it reloaded"

        folder = Path(config.sources[0].path)
        (folder / "second.md").write_text(
            "# Second\n\nThe failover drill runs quarterly.\n", encoding="utf-8"
        )
        writer = SyncEngine(config)
        try:
            writer.sync_source("src0", "full")
        finally:
            writer.close()

        assert refresher.check_once() is True, "a newer graph on disk was not picked up"
        assert api.graph_builder.graph.number_of_nodes() > before
        # Idempotent: a second check with nothing new does not reload again.
        assert refresher.check_once() is False
    finally:
        api.close()


def test_no_refresher_is_built_for_a_role_that_indexes(tmp_path: Path) -> None:
    from pheasant.cli import _graph_refresher
    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path, state_name="norefresh", sources=1, queue=True)
    engine = SyncEngine(config)
    try:
        for role in ("all", "indexer", "worker"):
            assert _graph_refresher(config, engine, resolve_role(config, role)) is None
        assert _graph_refresher(config, engine, resolve_role(config, "api")) is not None

        # And it is switchable off, for a deployment where the graph is not
        # shared between pods at all.
        config.server.api.graph_refresh_seconds = 0
        assert _graph_refresher(config, engine, resolve_role(config, "api")) is None
    finally:
        engine.close()


def test_serve_refuses_a_misconfigured_role_at_the_cli(tmp_path: Path, capsys: Any) -> None:
    from pheasant.cli import main

    config = _config(tmp_path, state_name="cli-role", sources=1)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        f"  name: {config.pheasant.name}\n"
        f"  state_path: {config.pheasant.state_path}\n"
        f"  workspace_root: {config.pheasant.workspace_root}\n"
        f"  exports_path: {config.pheasant.exports_path}\n",
        encoding="utf-8",
    )

    assert main(["serve", "--config", str(config_path), "--role", "api"]) == 1
    assert "sync.queue.enabled" in capsys.readouterr().out


def test_background_services_start_only_for_their_roles(tmp_path: Path, monkeypatch: Any) -> None:
    """`_serve_app` is where the table stops being a docstring.

    Asserted by intercepting uvicorn rather than by reading the code, so a
    future refactor that moves a `.start()` outside its `if` is caught.
    """

    import pheasant.cli as cli

    started: dict[str, list[str]] = {}

    class Recorder:
        def __init__(self, label: str, bucket: list[str]) -> None:
            self.label = label
            self.bucket = bucket

        def start(self) -> None:
            self.bucket.append(self.label)

        def stop(self) -> None:
            pass

    def run_for(role: str) -> list[str]:
        bucket: list[str] = []
        started[role] = bucket
        config = _config(tmp_path, state_name=f"svc-{role}", sources=1, queue=True)
        monkeypatch.setattr(
            cli,
            "_sync_services",
            lambda engine, cfg, config_path=None: (
                Recorder("watcher", bucket),
                Recorder("scheduler", bucket),
            ),
        )
        monkeypatch.setattr(
            cli,
            "_queue_drainer",
            lambda cfg, path, policy: Recorder("drainer", bucket) if policy.drains_queue else None,
        )
        monkeypatch.setattr(
            cli,
            "_graph_refresher",
            lambda cfg, engine, policy: (
                Recorder("refresher", bucket) if policy.refreshes_graph else None
            ),
        )
        monkeypatch.setattr(cli, "_report_ui", lambda app_obj, cfg: bucket.append("ui"))
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        cli._serve_app(config, str(tmp_path / "pheasant.yaml"), role=role)
        return bucket

    assert sorted(run_for("all")) == ["scheduler", "ui", "watcher"]
    assert sorted(run_for("api")) == ["refresher", "ui"]
    assert sorted(run_for("indexer")) == ["drainer", "scheduler", "watcher"]
    assert sorted(run_for("worker")) == []


def test_a_thread_is_not_left_running_after_stop(tmp_path: Path) -> None:
    from pheasant.cli import _QueueDrainer

    config = _config(tmp_path, state_name="threads", sources=1, queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")

    from pheasant.config.loader import load_config

    before = {thread.name for thread in threading.enumerate()}
    drainer = _QueueDrainer(load_config(config_path), str(config_path))
    drainer.start()
    time.sleep(0.5)
    drainer.stop()
    time.sleep(0.5)

    leaked = {thread.name for thread in threading.enumerate()} - before
    assert "pheasant-drainer" not in leaked
