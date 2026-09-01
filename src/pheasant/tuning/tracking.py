"""Experiment tracking, with MLflow as a mirror rather than a dependency.

MLflow is the right tool for the human half of this job: comparing runs,
plotting a parameter against a metric, and keeping a registry of packaged
configurations. It is the wrong tool to make load-bearing here, for reasons
that are specific to what pheasant is rather than to MLflow.

A region is **local-first, offline by default, and its state is user data**.
The suite is network-free by construction. A default install carries no
tracking server, and pillar 4 says operational truth lives in ``/state``. A
tracking store that had to exist for a batch to run would put a region's
retrieval configuration behind a service that most deployments will not have.

So the relationship is inverted from the usual one. **``/state`` is the source
of truth and MLflow is a projection of it.** Every experiment, trial, decision
and bundle is written to the database first; the sink then mirrors it. Losing
the mirror loses a UI, not a result -- and a region can turn tracking on later
and backfill, because the rows are all still there.

Three sinks, and the composite is what the runner actually holds:

``StateSink``   always on, writes the durable rows. Not optional.
``MlflowSink``  behind the ``[tuning]`` extra and ``tuning.tracking.backend:
                mlflow``. Defaults to a **local file store** under
                ``/exports/tuning/mlruns`` -- no server, no network, no
                credentials -- and accepts a ``tracking_uri`` for regions that
                do run one.
``NullSink``    the default. Named, rather than represented by ``None``, so
                every call site is unconditional and there is no ``if sink``
                sprinkled through the runner.

Every sink method swallows its own failures. A tracking backend that is down,
misconfigured, or mid-upgrade must not fail a tuning batch: the numbers are in
the database either way, and losing an experiment because a mirror was
unreachable would be the tail wagging the dog.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.tuning.contracts import Decision, Diagnosis, Experiment, Trial, TuningBundle
from pheasant.tuning.store import PRIMARY_METRIC

logger = logging.getLogger(__name__)

#: Where a file-store MLflow run tree lands, alongside the cold payloads. Both
#: are regenerable projections of `/state`, so they belong on the same volume
#: and under the same retention.
MLRUNS_SUBDIR = "mlruns"


class TrackingSink:
    """The interface. Every method is best-effort and returns nothing."""

    name = "null"

    def start_experiment(self, experiment: Experiment, space: dict[str, Any]) -> None: ...

    def log_diagnosis(self, experiment: Experiment, diagnosis: Diagnosis) -> None: ...

    def log_trial(self, experiment: Experiment, trial: Trial, cold_ref: str = "") -> None: ...

    def log_decision(self, experiment: Experiment, decision: Decision) -> None: ...

    def log_bundle(self, experiment: Experiment, bundle: TuningBundle) -> None: ...

    def finish(self, experiment: Experiment, status: str) -> None: ...


class NullSink(TrackingSink):
    """Tracking off. The default, and a complete implementation."""


class StateSink(TrackingSink):
    """The durable sink: rows in ``/state``. Always present.

    Deliberately a *sink* rather than direct calls from the runner. It makes
    the runner's write path uniform, and it makes the ordering explicit: this
    sink is always first in the composite, so the database write happens before
    any mirror sees the object. A mirror that raised could otherwise prevent
    the durable write it was supposed to be describing.
    """

    name = "state"

    def __init__(self, state: Any, kb_id: str, exports_path: Any = None):
        self.state = state
        self.kb_id = kb_id
        self.exports_path = exports_path

    def log_trial(self, experiment: Experiment, trial: Trial, cold_ref: str = "") -> None:
        from pheasant.tuning import store

        store.save_trial(self.state, trial, self.kb_id, cold_ref=cold_ref)

    def log_decision(self, experiment: Experiment, decision: Decision) -> None:
        from pheasant.tuning import store

        store.save_decision(self.state, decision, self.kb_id)

    def log_bundle(self, experiment: Experiment, bundle: TuningBundle) -> None:
        from pheasant.tuning import store

        store.save_bundle(self.state, bundle)


class MlflowSink(TrackingSink):
    """Mirror an experiment into MLflow. Optional, and never load-bearing.

    One MLflow *experiment* per knowledge base and one *run* per trial, nested
    under a parent run for the batch. That shape is chosen so the thing MLflow
    is genuinely good at -- "plot ``rrf_k`` against the primary metric across
    every trial" -- is one click rather than a query.

    The packaged configuration is logged as an artifact on the parent run, so
    an MLflow-shaped workflow can retrieve a bundle the same way it retrieves a
    model. It is a JSON document, not a pickled estimator: what this plane
    produces is a configuration set, and dressing it as a model artifact would
    invite somebody to try to ``mlflow.pyfunc.load_model`` it.
    """

    name = "mlflow"

    def __init__(
        self,
        *,
        tracking_uri: str = "",
        experiment_name: str = "pheasant-retrieval-tuning",
        exports_path: Any = None,
        tags: dict[str, str] | None = None,
    ):
        self._mlflow: Any = None
        self._parent: Any = None
        self._tags = dict(tags or {})
        self._experiment_name = experiment_name
        try:
            import mlflow  # noqa: PLC0415 - optional extra, imported on use
        except ImportError:
            logger.warning(
                "tuning: tracking.backend is 'mlflow' but mlflow is not installed; "
                "install the [tuning] extra. The batch will run and its results are "
                "still written to /state -- only the mirror is missing."
            )
            return
        uri = tracking_uri
        if not uri and exports_path:
            from pathlib import Path

            from pheasant.tuning.store import COLD_SUBDIR

            # A local file store, which is what makes this usable in a region
            # with no network: mlflow writes a run tree of plain files, and
            # `mlflow ui --backend-store-uri` opens it later with nothing
            # running in between.
            root = Path(exports_path) / COLD_SUBDIR / MLRUNS_SUBDIR
            root.mkdir(parents=True, exist_ok=True)
            uri = root.resolve().as_uri()
        if uri:
            mlflow.set_tracking_uri(uri)
        self._mlflow = mlflow

    def _safe(self, what: str, fn: Any) -> None:
        if self._mlflow is None:
            return
        try:
            fn()
        except Exception:  # noqa: BLE001 - a mirror must never fail a batch
            logger.warning("tuning: mlflow %s failed; results are still in /state", what)
            logger.debug("tuning: mlflow %s traceback", what, exc_info=True)

    def start_experiment(self, experiment: Experiment, space: dict[str, Any]) -> None:
        def run() -> None:
            self._mlflow.set_experiment(self._experiment_name)
            self._parent = self._mlflow.start_run(
                run_name=experiment.experiment_id,
                tags={
                    "pheasant.kb_id": experiment.kb_id,
                    "pheasant.snapshot_id": experiment.snapshot_id,
                    "pheasant.space_digest": experiment.space_digest,
                    "pheasant.plane": "tuning",
                    **self._tags,
                },
            )
            self._mlflow.log_params(
                {f"baseline.{k}": v for k, v in experiment.baseline_point.values.items()}
            )
            self._mlflow.log_params({f"budget.{k}": v for k, v in experiment.budget.items()})
            self._mlflow.log_dict(space, "parameter_space.json")

        self._safe("start_run", run)

    def log_diagnosis(self, experiment: Experiment, diagnosis: Diagnosis) -> None:
        def run() -> None:
            self._mlflow.log_dict(diagnosis.as_dict(), "diagnosis.json")
            # The histogram as metrics too, not just an artifact: "how many
            # misses were in fusion" is the number an operator wants on the
            # same axis as the trial scores, and an artifact cannot be plotted.
            for stage, count in (diagnosis.histogram.get("counts") or {}).items():
                self._mlflow.log_metric(f"diagnosis.{stage}", float(count))

        self._safe("log_diagnosis", run)

    def log_trial(self, experiment: Experiment, trial: Trial, cold_ref: str = "") -> None:
        def run() -> None:
            with self._mlflow.start_run(run_name=trial.trial_id, nested=True):
                self._mlflow.log_params(trial.proposal.point.values)
                self._mlflow.set_tags(
                    {
                        "pheasant.cohort": trial.cohort_name,
                        "pheasant.cost_class": trial.proposal.cost_class,
                        "pheasant.motivating_stage": trial.proposal.motivating_stage,
                        "pheasant.generation": str(trial.proposal.generation),
                        "pheasant.delta": trial.proposal.point.describe_delta(),
                        # The audit trail's outbound link: the mirror points
                        # back at the cold payload, so a plot that raises a
                        # question resolves to the ranked lists behind it.
                        "pheasant.cold_ref": cold_ref,
                        "pheasant.rationale": trial.proposal.rationale[:480],
                    }
                )
                self._mlflow.log_metrics({k: float(v) for k, v in trial.metrics.items()})
                self._mlflow.log_metrics(
                    {
                        "evaluated_queries": float(trial.evaluated_queries),
                        "excluded_queries": float(trial.excluded_queries),
                        "searches": float(trial.searches),
                        "duration_ms": float(trial.duration_ms),
                    }
                )

        self._safe("log_trial", run)

    def log_decision(self, experiment: Experiment, decision: Decision) -> None:
        def run() -> None:
            self._mlflow.log_dict(decision.as_dict(), "decision.json")
            self._mlflow.set_tags(
                {
                    "pheasant.outcome": decision.outcome,
                    "pheasant.gates_passed": str(decision.gates_passed),
                    "pheasant.reason": decision.reason[:480],
                }
            )

        self._safe("log_decision", run)

    def log_bundle(self, experiment: Experiment, bundle: TuningBundle) -> None:
        def run() -> None:
            self._mlflow.log_dict(bundle.as_dict(), "bundle.json")
            self._mlflow.log_dict(
                {"search": {"ranking": bundle.parameters}}, "pheasant.ranking.yaml.json"
            )
            self._mlflow.set_tag("pheasant.bundle_id", bundle.bundle_id)
            if bundle.metrics.get(PRIMARY_METRIC) is not None:
                self._mlflow.log_metric(
                    f"bundle.{PRIMARY_METRIC}", float(bundle.metrics[PRIMARY_METRIC])
                )

        self._safe("log_bundle", run)

    def finish(self, experiment: Experiment, status: str) -> None:
        def run() -> None:
            self._mlflow.set_tag("pheasant.status", status)
            if self._parent is not None:
                self._mlflow.end_run(status="FINISHED" if status == "completed" else "FAILED")
                self._parent = None

        self._safe("end_run", run)


class CompositeSink(TrackingSink):
    """Several sinks as one, in order, each isolated from the others.

    Order is the contract: :class:`StateSink` is first, so the durable write
    always happens before any mirror is offered the object. And one sink's
    failure never reaches the next -- a mirror that raises must not prevent a
    second mirror from recording, and neither may prevent the batch from
    continuing.
    """

    name = "composite"

    def __init__(self, sinks: list[TrackingSink]):
        self.sinks = [sink for sink in sinks if sink is not None]

    def _fan(self, method: str, *args: Any, **kwargs: Any) -> None:
        for sink in self.sinks:
            try:
                getattr(sink, method)(*args, **kwargs)
            except Exception:  # noqa: BLE001
                logger.warning("tuning: sink %s.%s failed", sink.name, method, exc_info=True)

    def start_experiment(self, experiment: Experiment, space: dict[str, Any]) -> None:
        self._fan("start_experiment", experiment, space)

    def log_diagnosis(self, experiment: Experiment, diagnosis: Diagnosis) -> None:
        self._fan("log_diagnosis", experiment, diagnosis)

    def log_trial(self, experiment: Experiment, trial: Trial, cold_ref: str = "") -> None:
        self._fan("log_trial", experiment, trial, cold_ref)

    def log_decision(self, experiment: Experiment, decision: Decision) -> None:
        self._fan("log_decision", experiment, decision)

    def log_bundle(self, experiment: Experiment, bundle: TuningBundle) -> None:
        self._fan("log_bundle", experiment, bundle)

    def finish(self, experiment: Experiment, status: str) -> None:
        self._fan("finish", experiment, status)


def sink_for(config: Any, state: Any) -> CompositeSink:
    """The sink stack a runner should hold, from ``tuning.tracking``.

    Always includes the state sink. An unknown or unavailable backend degrades
    to state-only with a warning rather than raising: a typo in a tracking URI
    should cost an operator their dashboard, not their tuning pass.
    """

    kb_id = str(getattr(config, "knowledge_base_id", "") or "")
    exports = getattr(getattr(config, "pheasant", None), "exports_path", None)
    sinks: list[TrackingSink] = [StateSink(state, kb_id, exports_path=exports)]

    tracking = getattr(getattr(config, "tuning", None), "tracking", None)
    backend = str(getattr(tracking, "backend", "off") or "off")
    if backend == "mlflow":
        sinks.append(
            MlflowSink(
                tracking_uri=str(getattr(tracking, "tracking_uri", "") or ""),
                experiment_name=str(
                    getattr(tracking, "experiment_name", "") or "pheasant-retrieval-tuning"
                ),
                exports_path=exports,
                tags={"pheasant.kb_id": kb_id},
            )
        )
    elif backend not in ("off", "state"):
        logger.warning(
            "tuning: unknown tracking backend %r; results are written to /state as always",
            backend,
        )
    return CompositeSink(sinks)
