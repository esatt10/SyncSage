"""Phase 35.6c — the role-split deployment manifests.

A manifest is code that runs on someone else's cluster, where a typo surfaces
as a CrashLoopBackOff rather than a stack trace. These tests check the things
that would only fail there: that the embedded configs are valid for the role
their workload passes, that the roles named in `args` exist, and that the
timing values which must agree across two files actually do.

Deliberately not tested: that Kubernetes accepts the manifests. That needs a
cluster or a schema bundle, and `kubeconform` in CI is the right place for it.
What is testable offline is the part that couples the manifests to *this*
codebase, and that is what is here.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.deployment.roles import Role, resolve_role, validate_role

REPO_ROOT = Path(__file__).resolve().parents[1]
SCALED = REPO_ROOT / "deploy" / "kubernetes" / "scaled"
BASE = REPO_ROOT / "deploy" / "kubernetes"


def _real_yaml():
    """Load the installed PyYAML rather than the repo-root shim.

    `yaml.py` at the checkout root is a dependency-light stand-in, and it
    shadows PyYAML for anything running from there — including pytest. It
    parses the single-document configs it was written for and not much else;
    a multi-document manifest with nested lists trips it. So these tests load
    the real library by removing the root from `sys.path` for one import,
    which is also the reason the same trick appears in the docs build.
    """

    saved = sys.modules.pop("yaml", None)
    original = sys.path[:]
    sys.path[:] = [entry for entry in sys.path if entry not in ("", ".", str(REPO_ROOT))]
    try:
        return importlib.import_module("yaml")
    finally:
        sys.path[:] = original
        if saved is not None:
            sys.modules["yaml"] = saved


yaml = _real_yaml()


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _all_documents(directory: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        docs.extend(_documents(path))
    return docs


def _by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _by_kind(docs, "Deployment") + _by_kind(docs, "StatefulSet")


def _pod_spec(workload: dict[str, Any]) -> dict[str, Any]:
    return workload["spec"]["template"]["spec"]


def _container(workload: dict[str, Any]) -> dict[str, Any]:
    return _pod_spec(workload)["containers"][0]


@pytest.fixture(scope="module")
def scaled() -> list[dict[str, Any]]:
    return _all_documents(SCALED)


# --------------------------------------------------------------------------
# The manifests describe a fleet this code can actually run
# --------------------------------------------------------------------------


def test_every_manifest_parses(scaled: list[dict[str, Any]]) -> None:
    assert scaled, "no manifests found"
    for doc in scaled:
        assert doc.get("kind"), f"document without a kind: {doc}"
        assert doc.get("metadata", {}).get("name")


def test_every_workload_passes_a_role_this_code_knows(scaled: list[dict[str, Any]]) -> None:
    """A role typo in `args` is a CrashLoopBackOff on someone else's cluster."""

    valid = {role.value for role in Role}
    seen = set()
    for workload in _workloads(scaled):
        args = _container(workload)["args"]
        assert args[0] == "serve", f"{workload['metadata']['name']} does not run `serve`"
        assert args[1] == "--role"
        assert args[2] in valid, f"unknown role {args[2]!r}"
        seen.add(args[2])
    assert seen == {"api", "indexer", "worker"}


def test_the_embedded_configs_are_valid_for_the_role_that_uses_them(
    scaled: list[dict[str, Any]],
) -> None:
    """The api role refuses to start without a queue — so the config must have one.

    This is the check that would have caught shipping a fleet ConfigMap with
    `sync.queue.enabled` left at its default: `validate_role` is the same
    function the process runs at startup.
    """

    configs = {
        doc["metadata"]["name"]: PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        for raw in doc["data"].values()
    }
    assert set(configs) == {"pheasant-fleet-config", "pheasant-worker-config"}

    for workload in _workloads(scaled):
        role = _container(workload)["args"][2]
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        name = volumes["config"]["configMap"]["name"]
        config = configs[name]
        validate_role(resolve_role(config, role), config)


def test_the_api_and_indexer_share_one_knowledge_base(scaled: list[dict[str, Any]]) -> None:
    """Two names would be two knowledge bases, and every stable ID starts with it."""

    fleet = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-fleet-config"
        for raw in doc["data"].values()
    )
    api, indexer = (
        next(w for w in _workloads(scaled) if _container(w)["args"][2] == role)
        for role in ("api", "indexer")
    )
    for workload in (api, indexer):
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        assert volumes["config"]["configMap"]["name"] == "pheasant-fleet-config"
    assert fleet.pheasant.name


# --------------------------------------------------------------------------
# The trust boundary
# --------------------------------------------------------------------------


def test_workers_never_receive_the_database_dsn(scaled: list[dict[str, Any]]) -> None:
    """A worker parses bytes. It has no reason to reach the knowledge base.

    The whole argument for autoscaling workers hard and killing them freely is
    that they hold nothing; handing them a DSN would quietly retract it.
    """

    worker = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "worker")
    env_names = {entry["name"] for entry in _container(worker).get("env", [])}

    assert "PHEASANT_DATABASE_URL" not in env_names
    assert "PHEASANT_INDEX_WORKER_TOKEN" in env_names, "a worker cannot authenticate without it"


def test_no_secret_values_are_baked_into_a_configmap(scaled: list[dict[str, Any]]) -> None:
    """Only env-var *names* reach YAML — the house rule for every credential."""

    for doc in _by_kind(scaled, "ConfigMap"):
        for raw in doc["data"].values():
            config = PheasantConfig.model_validate(yaml.safe_load(raw))
            assert getattr(config.storage, "dsn", None) in (None, "")
            assert "postgresql://" not in raw, "a DSN was written into a ConfigMap"
            # The env var is referenced by name, which is the whole point.
            if config.storage.backend == "postgres":
                assert config.storage.dsn_env


def test_every_pod_drops_capabilities_and_runs_as_non_root(scaled: list[dict[str, Any]]) -> None:
    for workload in _workloads(scaled):
        pod = _pod_spec(workload)
        assert pod["securityContext"]["runAsNonRoot"] is True
        container = _container(workload)["securityContext"]
        assert container["allowPrivilegeEscalation"] is False
        assert container["readOnlyRootFilesystem"] is True
        assert container["capabilities"]["drop"] == ["ALL"]


def test_api_replicas_mount_state_read_only(scaled: list[dict[str, Any]]) -> None:
    """One writer of /state. The graph is a file, and torn writes are forever."""

    api = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "api")
    mounts = {mount["name"]: mount for mount in _container(api)["volumeMounts"]}
    assert mounts["state"].get("readOnly") is True

    indexer = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "indexer")
    indexer_mounts = {mount["name"]: mount for mount in _container(indexer)["volumeMounts"]}
    assert not indexer_mounts["state"].get("readOnly"), "the indexer must be able to write /state"


def test_the_shared_state_volume_is_readwritemany(scaled: list[dict[str, Any]]) -> None:
    """RWO attaches to one node, so api replicas elsewhere cannot read the graph.

    A hard requirement rather than a preference, and the one most likely to be
    skipped — most default StorageClasses are RWO.
    """

    claims = {doc["metadata"]["name"]: doc for doc in _by_kind(scaled, "PersistentVolumeClaim")}
    assert claims["pheasant-state"]["spec"]["accessModes"] == ["ReadWriteMany"]


# --------------------------------------------------------------------------
# Values that must agree across two files
# --------------------------------------------------------------------------


def test_the_drain_delay_fits_inside_the_termination_grace_period(
    scaled: list[dict[str, Any]],
) -> None:
    """A drain longer than the grace period is a pod killed mid-drain.

    The two numbers live in different files — one in a ConfigMap, one in a pod
    spec — which is exactly the kind of coupling nothing else would notice.
    """

    configs = {
        doc["metadata"]["name"]: PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        for raw in doc["data"].values()
    }
    for workload in _workloads(scaled):
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        config = configs[volumes["config"]["configMap"]["name"]]
        drain = config.server.api.drain_seconds
        grace = _pod_spec(workload)["terminationGracePeriodSeconds"]
        assert drain < grace, (
            f"{workload['metadata']['name']}: drain_seconds={drain} is not shorter than "
            f"terminationGracePeriodSeconds={grace}, so the pod is killed mid-drain"
        )


def test_the_indexer_is_a_single_unscaled_replica(scaled: list[dict[str, Any]]) -> None:
    """Indexing one source is lease-serialized; a second replica loses races."""

    indexer = next(
        doc for doc in _by_kind(scaled, "StatefulSet") if _container(doc)["args"][2] == "indexer"
    )
    assert indexer["spec"]["replicas"] == 1

    scaled_names = {
        doc["spec"]["scaleTargetRef"]["name"] for doc in _by_kind(scaled, "HorizontalPodAutoscaler")
    } | {doc["spec"]["scaleTargetRef"]["name"] for doc in _by_kind(scaled, "ScaledObject")}
    assert "pheasant-indexer" not in scaled_names


def test_the_indexer_pdb_permits_a_node_drain(scaled: list[dict[str, Any]]) -> None:
    """minAvailable: 1 on a single replica blocks every drain, forever.

    Safe because an evicted indexer's in-flight task is redelivered by the
    queue's visibility timeout — a drain costs latency, not work.
    """

    pdbs = {doc["metadata"]["name"]: doc for doc in _by_kind(scaled, "PodDisruptionBudget")}
    assert pdbs["pheasant-indexer"]["spec"]["minAvailable"] == 0
    assert pdbs["pheasant-api"]["spec"]["minAvailable"] == 1


def test_the_worker_autoscaler_reads_the_queue_depth_metric(scaled: list[dict[str, Any]]) -> None:
    """CPU is a lagging signal here; the backlog is the leading one.

    Also asserts the metric name against the registry, so renaming the series
    without updating the manifests fails here rather than in production.
    """

    from pheasant.telemetry import metrics

    metrics.register_default_metrics("test")
    assert "pheasant_index_queue_depth" in metrics.REGISTRY.render()

    keda = next(doc for doc in _by_kind(scaled, "ScaledObject"))
    assert keda["spec"]["scaleTargetRef"]["name"] == "pheasant-worker"
    assert "pheasant_index_queue_depth" in keda["spec"]["triggers"][0]["metadata"]["query"]
    assert keda["spec"]["minReplicaCount"] == 0, "an idle fleet should not pay for workers"

    hpa = next(
        doc
        for doc in _by_kind(scaled, "HorizontalPodAutoscaler")
        if doc["spec"]["scaleTargetRef"]["name"] == "pheasant-worker"
    )
    external = [entry for entry in hpa["spec"]["metrics"] if entry["type"] == "External"]
    assert external[0]["external"]["metric"]["name"] == "pheasant_index_queue_depth"


def test_the_indexer_points_at_the_worker_service(scaled: list[dict[str, Any]]) -> None:
    """The coordinator must address workers by Service, not by pod.

    Pod IPs change on every reschedule; the Service is what lets the HPA add
    and remove replicas without anyone editing config.
    """

    fleet = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-fleet-config"
        for raw in doc["data"].values()
    )
    urls = fleet.sync.concurrency.remote_worker_urls
    assert urls == ["http://pheasant-worker:8765"]

    services = {doc["metadata"]["name"] for doc in _by_kind(scaled, "Service")}
    assert "pheasant-worker" in services


def test_workers_actually_enable_the_preparation_endpoints(scaled: list[dict[str, Any]]) -> None:
    """Without this the routes 404 and every batch falls back to local parsing.

    Which would still *work* — that is the durability guarantee — and so would
    be invisible except as a fleet that costs money and does nothing.
    """

    worker_config = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-worker-config"
        for raw in doc["data"].values()
    )
    assert worker_config.sync.concurrency.remote_worker_enabled is True
    assert worker_config.sync.watcher.enabled is False
    assert worker_config.sync.scheduler.enabled is False


# --------------------------------------------------------------------------
# Schema validation, when a validator is available
# --------------------------------------------------------------------------


def _kubeconform() -> str | None:
    import shutil

    return shutil.which("kubeconform") or (
        "/tmp/kubeconform" if Path("/tmp/kubeconform").exists() else None
    )


kubeconform = pytest.mark.skipif(
    _kubeconform() is None,
    reason="install kubeconform to validate manifests against Kubernetes schemas",
)


@kubeconform
@pytest.mark.parametrize("directory", [BASE, SCALED], ids=["base", "scaled"])
def test_manifests_validate_against_kubernetes_schemas(directory: Path) -> None:
    """The check I previously said belonged in CI, run here when it can be.

    The other tests in this file check the coupling to *pheasant* — that a
    role exists, that a config passes `validate_role`. This one checks the
    coupling to *Kubernetes*, which nothing in Python can: a misspelled field
    or a wrong apiVersion is a `kubectl apply` failure on someone else's
    cluster, and the offline tests would never see it.

    `-ignore-missing-schemas` because ServiceMonitor and ScaledObject are CRDs
    whose schemas ship with their operators; those four are skipped, and the
    assertion below pins that number so a *core* resource silently becoming
    unvalidatable is caught rather than absorbed.
    """

    import subprocess

    binary = _kubeconform()
    assert binary is not None
    result = subprocess.run(
        [binary, "-summary", "-strict", "-ignore-missing-schemas", *_manifest_paths(directory)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    summary = result.stdout.strip().splitlines()[-1]
    assert "Invalid: 0" in summary, summary
    assert "Errors: 0" in summary, summary
    expected_skips = "Skipped: 4" if directory == SCALED else "Skipped: 0"
    assert expected_skips in summary, f"unexpected skip count: {summary}"


def _manifest_paths(directory: Path) -> list[str]:
    return [str(path) for path in sorted(directory.glob("*.yaml"))]


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------

COMPOSE_FLEET = REPO_ROOT / "docker-compose.scale.yml"
COMPOSE_CONFIG = REPO_ROOT / "deploy" / "compose"


def test_the_compose_fleet_matches_the_kubernetes_shape() -> None:
    """Three tiers, the same roles, and only the worker meant to be scaled."""

    compose = yaml.safe_load(COMPOSE_FLEET.read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"postgres", "api", "indexer", "worker"}

    roles = {}
    for name in ("api", "indexer", "worker"):
        command = services[name]["command"]
        assert command[:2] == ["serve", "--role"]
        roles[name] = command[2]
    assert roles == {"api": "api", "indexer": "indexer", "worker": "worker"}

    # One indexer, pinned: two would take turns on one lease, not double up.
    assert services["indexer"]["scale"] == 1


def test_the_compose_worker_gets_no_database_url() -> None:
    """Same trust boundary as the Kubernetes manifests, checked the same way."""

    compose = yaml.safe_load(COMPOSE_FLEET.read_text(encoding="utf-8"))
    env = compose["services"]["worker"]["environment"]
    assert "PHEASANT_DATABASE_URL" not in env
    assert "PHEASANT_INDEX_WORKER_TOKEN" in env


def test_the_compose_configs_are_valid_for_their_roles() -> None:
    fleet = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "fleet.yaml").read_text(encoding="utf-8"))
    )
    worker = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "worker.yaml").read_text(encoding="utf-8"))
    )

    for role in ("api", "indexer"):
        validate_role(resolve_role(fleet, role), fleet)
    validate_role(resolve_role(worker, "worker"), worker)

    assert fleet.sync.queue.enabled is True
    assert fleet.storage.backend == "postgres"
    # Addressed by Compose service name, so `--scale worker=N` needs no edit.
    assert fleet.sync.concurrency.remote_worker_urls == ["http://worker:8765"]
    assert worker.sync.concurrency.remote_worker_enabled is True
    assert fleet.server.api.drain_seconds > 0


def test_the_default_compose_file_is_still_one_container() -> None:
    """Rule 7: `docker compose up` must keep needing no infrastructure."""

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "postgres" not in services
    assert "worker" not in services
    # The UI sidecar is the only extra, and it is behind a profile.
    assert services["pheasant-ui"]["profiles"] == ["ui"]
    assert "command" not in services["pheasant"], "the default container must not pass a role"


# --------------------------------------------------------------------------
# The single-container install stays the default
# --------------------------------------------------------------------------


def test_the_base_install_still_needs_no_infrastructure() -> None:
    """Rule 7 at the deployment layer: `kubectl apply -f deploy/kubernetes/`
    must keep working with no Postgres, no broker and no RWX class."""

    docs = _all_documents(BASE)
    config = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(docs, "ConfigMap")
        for raw in doc["data"].values()
    )
    assert config.storage.backend == "sqlite"
    assert config.sync.queue.enabled is False
    assert config.server.role == "all"
    validate_role(resolve_role(config), config)

    claims = _by_kind(docs, "PersistentVolumeClaim")
    assert all(claim["spec"]["accessModes"] == ["ReadWriteOnce"] for claim in claims)

    # And no workload in the base install passes --role: the default is `all`.
    for workload in _workloads(docs):
        assert "--role" not in (_container(workload).get("args") or [])
