"""The evaluation plane: measuring whether this region is getting better.

Records *about* the knowledge base, never part of it. Nothing this package
writes is a file, is chunked, is indexed, or is returned by an ordinary search
-- the same boundary the observation plane draws, and drawn again here because
the temptation is stronger: an evaluation report reads like knowledge.

The public surface is small on purpose:

* :func:`record_evidence` -- the one way typed proof enters the system.
* :func:`run` -- the batch, fleet-safe and read-only unless promotion is on.
* :func:`latest_report`, :func:`trend` -- reading what previous runs found.

Everything else is a module: ``snapshots``, ``proof``, ``cohorts``,
``variants``, ``replay``, ``metrics``, ``gates``, ``report``, ``candidates``,
``runner``, ``store``. ``docs/knowledge-effectiveness.md`` is the prose.
"""

from __future__ import annotations

from typing import Any

from pheasant.evaluation.contracts import (
    EVALUATION_SCHEMA_VERSION,
    Classification,
    MetricStatus,
    Polarity,
    Strength,
    query_id,
)
from pheasant.evaluation.proof import (
    DEFAULT_TAXONOMY,
    ProofPolicy,
    make_proof,
    partition_token,
)
from pheasant.evaluation.runner import (
    RunOutcome,
    reclaim_interrupted_runs,
    run_evaluation,
)
from pheasant.evaluation.store import (
    active_run,
    latest_run,
    list_runs,
    load_report,
    metric_trend,
    run_status,
)

__all__ = [
    "DEFAULT_TAXONOMY",
    "EVALUATION_SCHEMA_VERSION",
    "Classification",
    "MetricStatus",
    "Polarity",
    "ProofPolicy",
    "RunOutcome",
    "Strength",
    "latest_report",
    "latest_run",
    "list_runs",
    "progress",
    "query_id",
    "reclaim_interrupted_runs",
    "record_evidence",
    "run",
    "run_status",
    "trend",
]


def record_evidence(
    state: Any,
    config: Any,
    *,
    query: str,
    target_id: str,
    event_type: str,
    target_type: str = "artifact",
    interaction_id: str | None = None,
    principal: str | None = None,
    session_id: str | None = None,
    position: int | None = None,
    outcome_reference: str | None = None,
    reason_code: str = "",
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Record one typed observation about one target. The only way proof enters.

    Deliberately a single call with a required ``event_type`` drawn from the
    taxonomy, rather than a "mark this useful" convenience. The distinction
    between *cited*, *selected*, *accepted* and *validated* is the difference
    between four claims of very different strength, and an API that let a
    caller skip it would collapse them at the point where the information still
    exists.

    Unknown event types are rejected rather than stored: a proof row naming an
    event nobody has weighted is a row no metric can read, and finding out at
    metric time is finding out too late.
    """

    if event_type not in DEFAULT_TAXONOMY:
        raise ValueError(
            f"Unknown evidence event type: {event_type}. "
            f"Known types: {', '.join(sorted(DEFAULT_TAXONOMY))}"
        )
    from pheasant.evaluation import store as evaluation_store

    policy = ProofPolicy.from_config(getattr(config.evaluation, "proof", None))
    proof = make_proof(
        kb_id=config.knowledge_base_id,
        query_text=query,
        target_type=target_type,
        target_id=target_id,
        event_type=event_type,
        policy=policy,
        observed_at=observed_at,
        interaction_id=interaction_id,
        principal_partition=partition_token(config.knowledge_base_id, principal, session_id),
        position=position,
        outcome_reference=outcome_reference,
        reason_code=reason_code,
    )
    written = evaluation_store.save_proofs(state, [proof])
    return {
        "proof_id": proof.proof_id,
        "query_id": proof.query_id,
        "polarity": proof.polarity,
        "strength": proof.strength,
        "weight": proof.weight,
        "multipliers": proof.multipliers,
        "recorded": bool(written),
    }


def run(engine: Any, **kwargs: Any) -> RunOutcome:
    """Run one evaluation batch. See :func:`pheasant.evaluation.runner.run_evaluation`."""

    return run_evaluation(engine, **kwargs)


def latest_report(state: Any, kb_id: str) -> dict[str, Any] | None:
    """The most recent completed run's report, or ``None``."""

    latest = latest_run(state, kb_id)
    if latest is None:
        return None
    return load_report(state, str(latest["run_id"]))


def trend(state: Any, kb_id: str, metric_id: str, **kwargs: Any) -> list[dict[str, Any]]:
    """One metric's history across snapshots, oldest first."""

    return metric_trend(state, kb_id, metric_id, **kwargs)


def progress(state: Any, kb_id: str, run_id: str | None = None) -> dict[str, Any]:
    """What a batch is doing right now, readable from any process.

    The answer comes from ``/state``, not from the process running the batch,
    which is what makes it work at all for the two cases that matter: a UI in a
    browser talking to an API replica that did not start the run, and a watcher
    reconnecting after the container that *was* running it stopped.

    Without ``run_id`` this reports the batch in flight, or the last one that
    finished. A run whose heartbeat has expired reads as ``interrupted`` once
    :func:`reclaim_interrupted_runs` has been round -- never as a spinner
    nobody will ever stop.
    """

    if run_id:
        found = run_status(state, run_id)
        return found or {"run_id": run_id, "status": "unknown"}
    live = active_run(state, kb_id)
    if live is not None:
        return live
    latest = latest_run(state, kb_id)
    if latest is None:
        return {"status": "none", "detail": "no evaluation batch has run for this knowledge base"}
    return run_status(state, str(latest["run_id"])) or {"status": "none"}
