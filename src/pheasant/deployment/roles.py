"""Process roles (Phase 35.6).

One pheasant process does everything: serves search, serves the UI, watches
the filesystem, runs the scheduler, and indexes. That is exactly right for a
single container and exactly wrong for a fleet, for one reason that has
nothing to do with performance: **replicas.** Run three copies of today's
process against one knowledge base and all three watch the same directories,
all three fire the same scheduled sync, and all three try to index the same
source. The Phase 35.4 leases stop that from *corrupting* anything, but three
processes taking turns to do one process's work is not horizontal scale.

A role says which of those jobs this process has:

===========  ======  =========  =====  =========  ==============================
Role         Watch   Schedule   Drain  Index      Typical deployment
===========  ======  =========  =====  =========  ==============================
``all``      yes     yes        no     in-proc    one container (the default)
``api``      no      no         no     **never**  N replicas behind a Service
``indexer``  yes     yes        yes    in-proc    one per shard, a StatefulSet
``graph``    no      no         no     no         internal graph query service
``worker``   no      no         no     no         M replicas, autoscaled
===========  ======  =========  =====  =========  ==============================

``all`` is the default and is **byte-identical to the behavior before roles
existed** — it does not drain a queue, because that would change what a
single container does the moment the queue is switched on for its crash
resumption alone.

The load-bearing cell is ``api``/Index/**never**. An api replica that indexed
would put every replica on the same source, which is the failure roles exist
to prevent; instead its sync requests are *published* and an indexer picks
them up. That makes the queue a hard requirement for that role, checked at
startup rather than discovered when a sync silently goes nowhere.

Routes are deliberately **not** hidden per role. The indexer coordinator does
not load the persisted graph in its parent process, however: sync children own
graph commits, and the Service selector keeps queries on API replicas. This
avoids holding two complete graph copies during every index while preserving
health, readiness, queue and control-plane diagnostics on the indexer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ALL = "all"
    API = "api"
    INDEXER = "indexer"
    GRAPH = "graph"
    WORKER = "worker"
    LOGGER = "logger"


@dataclass(frozen=True)
class RolePolicy:
    """What a process with this role does without being asked."""

    role: Role
    #: Watch source directories for changes and re-index what changed.
    runs_watcher: bool
    #: Fire the interval re-sync beat (which also carries memory/IdP upkeep).
    runs_scheduler: bool
    #: Claim tasks from the durable index queue.
    drains_queue: bool
    #: Index in this process (or its own child) rather than publishing.
    indexes_locally: bool
    #: Serve the bundled UI. False on roles nobody points a browser at.
    serves_ui: bool
    #: Poll ``/state`` for a graph written by *another* process and reload it.
    #:
    #: Only ``api`` needs this, and it needs it badly: the graph is a file, a
    #: process loads it once at startup, and the only existing reload happens
    #: after a sync **this** process ran. An api replica never indexes, so
    #: without polling it would serve graph queries against whatever the graph
    #: was when the pod started — indefinitely, and silently, while text and
    #: vector search stayed current from the shared database.
    refreshes_graph: bool = False
    #: Claim batches from the **log** queue -- a different table with a
    #: different failure mode, deliberately not the index queue.
    #:
    #: Its own flag rather than a reuse of ``drains_queue`` because the two
    #: must be independently settable: an ``indexer`` drains index work and
    #: should not also be doing hot-to-cold Parquet rolls, which is exactly
    #: the coupling that would put a multi-million-row roll inside the
    #: scheduler's ``sync_lock``.
    drains_log_queue: bool = False

    @property
    def name(self) -> str:
        return self.role.value

    @property
    def is_default(self) -> bool:
        return self.role is Role.ALL


POLICIES: dict[Role, RolePolicy] = {
    Role.ALL: RolePolicy(
        role=Role.ALL,
        runs_watcher=True,
        runs_scheduler=True,
        # Deliberately False: `all` must not change behavior when the queue is
        # switched on. A single container turns the queue on for crash
        # resumption, not to become a fleet, and `sync_all` already drains
        # what it publishes.
        drains_queue=False,
        # False for the same reason, and it is the same reason: `all` must
        # behave identically whether or not a queue exists. A single container
        # rolls its own logs inline on the maintenance beat, bounded by
        # `max_rows_per_pass`, rather than growing a second worker.
        drains_log_queue=False,
        indexes_locally=True,
        serves_ui=True,
    ),
    Role.API: RolePolicy(
        role=Role.API,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=True,
        refreshes_graph=True,
    ),
    Role.INDEXER: RolePolicy(
        role=Role.INDEXER,
        runs_watcher=True,
        runs_scheduler=True,
        drains_queue=True,
        indexes_locally=True,
        serves_ui=False,
    ),
    Role.GRAPH: RolePolicy(
        role=Role.GRAPH,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=False,
        # The indexer commits snapshots on shared state. Only this service
        # reloads them when APIs use the remote graph boundary.
        refreshes_graph=True,
    ),
    Role.WORKER: RolePolicy(
        role=Role.WORKER,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=False,
    ),
    # The log tier. Everything else is False on purpose: this process serves
    # no traffic, indexes nothing, holds no sync lock and loads no graph. That
    # is the entire point -- persistence, rolling and cold compaction for a
    # request-rate stream, in a failure domain that shares nothing with
    # serving or ingest.
    Role.LOGGER: RolePolicy(
        role=Role.LOGGER,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=False,
        drains_log_queue=True,
    ),
}


class RoleConfigurationError(ValueError):
    """A role and a config that cannot do what the role promises."""


def resolve_role(config: object, override: str | None = None) -> RolePolicy:
    """CLI flag beats config beats the default.

    An unknown name raises rather than falling back to ``all``: a typo in a
    Deployment's args that silently produced a full-service pod would put an
    indexer's watcher on every api replica, which is the exact thing roles
    exist to prevent — and it would look like it worked.
    """

    server = getattr(config, "server", None)
    name = (override or getattr(server, "role", None) or Role.ALL.value).strip().lower()
    try:
        role = Role(name)
    except ValueError as exc:
        valid = ", ".join(item.value for item in Role)
        raise RoleConfigurationError(f"Unknown role {name!r}. Valid roles: {valid}") from exc
    return POLICIES[role]


def validate_role(policy: RolePolicy, config: object) -> None:
    """Refuse a combination that cannot work, at startup.

    Two checks, and each earns its place by turning a silent no-op into a
    refusal. An ``api`` replica publishes its syncs instead of running them,
    so without a queue a sync request would be accepted and then go
    **nowhere** — the caller gets a job id, the UI shows a job, and nothing
    ever indexes. A ``logger`` with nothing to drain is the same shape: a pod
    that starts healthy, reports ready, and does nothing forever.
    """

    if policy.role is Role.API:
        queue = getattr(getattr(config, "sync", None), "queue", None)
        if not getattr(queue, "enabled", False):
            raise RoleConfigurationError(
                "role 'api' publishes index work instead of running it, so it needs "
                "sync.queue.enabled: true and an indexer draining the same queue. "
                "Without that, every sync request would be accepted and never run."
            )
    if policy.role is Role.LOGGER:
        interactions = getattr(getattr(config, "observability", None), "interactions", None)
        if not getattr(interactions, "enabled", False):
            raise RoleConfigurationError(
                "role 'logger' drains the observation log queue, so it needs "
                "observability.interactions.enabled: true. Without that there is "
                "nothing to drain and the process would idle forever while "
                "reporting itself healthy."
            )
        if not getattr(getattr(interactions, "queue", None), "enabled", False):
            raise RoleConfigurationError(
                "role 'logger' needs observability.interactions.queue.enabled: true. "
                "With the queue off, whichever process observed a call also writes "
                "it, so a separate log tier would have no work to claim."
            )


def describe(policy: RolePolicy) -> dict[str, object]:
    """Role facts for ``/ready`` and ``/health``, so a pod can be identified."""

    return {
        "role": policy.name,
        "watcher": policy.runs_watcher,
        "scheduler": policy.runs_scheduler,
        "drains_queue": policy.drains_queue,
        "drains_log_queue": policy.drains_log_queue,
        "indexes_locally": policy.indexes_locally,
        "refreshes_graph": policy.refreshes_graph,
    }
