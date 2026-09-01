"""The shapes the tuning plane persists, and the trail that connects them.

The requirement this module exists to satisfy is "traceable to every step and
decision reason", and traceability is not a logging convention -- it is a data
model or it is nothing. So the chain is closed by construction:

    proof --> query --> attribution --> diagnosis --> proposal --> trial
          --> comparison --> gate --> decision --> bundle --> applied overlay

Every arrow is a stored id, and every object below carries the id of the thing
that caused it. A served result names the bundle it ranked under
(:attr:`RankingParameters.bundle_id`); the bundle names the decision; the
decision names the gates it passed and the trials it compared; a trial names
the proposal; the proposal names the stage in the diagnosis that motivated it;
the diagnosis names the attributions; each attribution names the query and the
target. Ask "why is this document ranked here" and the answer is a walk, not a
reconstruction.

Two conventions borrowed from the evaluation plane, for the same reasons.

**Content-addressed ids, with no clock in them.** An experiment is its
(region, snapshot, space, cohort, budget) tuple, so re-running an unchanged
experiment is the *same* experiment rather than a second row that looks like a
second data point. The evaluation plane learned this the expensive way: a
clock-seeded run id made two runs a second apart into two rows and two runs
inside one second collapse into one.

**A result that cannot be explained is not published.** :meth:`Trial.validate`
is the mechanical version of that: a trial without a metric, a denominator and
a named parameter delta cannot be stored, so the omission surfaces at the call
site rather than in a report six weeks later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from pheasant.evaluation.contracts import digest, utc_now

__all__ = [
    "Comparison",
    "Decision",
    "Diagnosis",
    "Experiment",
    "ParameterPoint",
    "Proposal",
    "StageAttribution",
    "Trial",
    "TuningBundle",
    "digest",
    "utc_now",
]


@dataclass(frozen=True)
class StageAttribution:
    """Where one query lost one target. The atom of the diagnosis."""

    query_id: str
    target_id: str
    stage: str
    reason: str
    #: Whether a parameter in this package could plausibly move this stage.
    #: Carried per attribution rather than derived at read time so a stored
    #: diagnosis stays readable after the actionable set changes.
    actionable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Diagnosis:
    """The stage histogram for one cohort under one parameter point.

    This is the *baseline observation*, not a judgement. It says where the
    misses are; it does not say the region is bad, and it deliberately cannot
    produce a single number. A composite "retrieval health" score would be the
    exact artifact this plane exists to avoid -- it would go up when the
    dominant failure moved from one stage to another without anything getting
    better.
    """

    diagnosis_id: str
    kb_id: str
    snapshot_id: str
    cohort_id: str
    cohort_name: str
    baseline_point_id: str
    histogram: dict[str, Any]
    attributions: tuple[StageAttribution, ...] = ()
    unevidenced_queries: int = 0
    created_at: str = field(default_factory=utc_now)
    #: Plain-language reading of the histogram, including when it says "do not
    #: tune". Stored rather than rendered so the recommendation a decision was
    #: made under survives a later change to the wording.
    summary: str = ""

    @property
    def dominant_stage(self) -> str:
        return str(self.histogram.get("dominant_stage") or "")

    @property
    def actionable_share(self) -> float | None:
        share = self.histogram.get("actionable_share")
        return None if share is None else float(share)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attributions"] = [item.as_dict() for item in self.attributions]
        return payload


@dataclass(frozen=True)
class ParameterPoint:
    """One complete parameter assignment, addressed by its values.

    The id is a digest of the values, so the same point proposed twice by two
    different strategies is one point with one trial -- which is what makes a
    coordinate-descent sweep that revisits a coordinate cost nothing.
    """

    point_id: str
    values: dict[str, float]
    #: What this differs from, and how. Empty for the baseline.
    parent_point_id: str = ""
    delta: dict[str, tuple[float, float]] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        values: dict[str, float],
        *,
        parent: ParameterPoint | None = None,
    ) -> ParameterPoint:
        ordered = {name: float(values[name]) for name in sorted(values)}
        point_id = "pt-" + digest(sorted(ordered.items()))
        delta: dict[str, tuple[float, float]] = {}
        if parent is not None:
            delta = {
                name: (parent.values.get(name, value), value)
                for name, value in ordered.items()
                if parent.values.get(name) != value
            }
        return cls(
            point_id=point_id,
            values=ordered,
            parent_point_id=parent.point_id if parent else "",
            delta=delta,
        )

    def describe_delta(self) -> str:
        if not self.delta:
            return "baseline"
        return ", ".join(
            f"{name}: {before:g} -> {after:g}"
            for name, (before, after) in sorted(self.delta.items())
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "values": dict(self.values),
            "parent_point_id": self.parent_point_id,
            "delta": {name: list(pair) for name, pair in self.delta.items()},
            "delta_description": self.describe_delta(),
        }


@dataclass(frozen=True)
class Proposal:
    """A point, plus the reason it was worth trying.

    ``motivating_stage`` is load-bearing rather than decorative: the strategy
    only proposes parameters whose stage the diagnosis blames, and this field
    is the record of that link. A proposal that cannot name the stage it is
    meant to fix has no business consuming a trial budget.
    """

    point: ParameterPoint
    motivating_stage: str
    rationale: str
    #: ``refusion`` (evaluated from cached arm lists, effectively free) or
    #: ``requery`` (needs a real retrieval per query). The two are budgeted
    #: separately because their costs differ by three orders of magnitude.
    cost_class: str = "refusion"
    strategy: str = ""
    generation: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "point": self.point.as_dict(),
            "motivating_stage": self.motivating_stage,
            "rationale": self.rationale,
            "cost_class": self.cost_class,
            "strategy": self.strategy,
            "generation": self.generation,
        }


@dataclass
class Trial:
    """One proposal, evaluated on one cohort.

    Metrics are stored per k rather than as a single score. A point that lifts
    recall at 10 while dropping the reciprocal rank has moved a document from
    "not found" to "found at rank 9", which may or may not be an improvement
    depending on what reads the results -- and a single number hides exactly
    that.
    """

    trial_id: str
    experiment_id: str
    proposal: Proposal
    cohort_id: str
    cohort_name: str
    metrics: dict[str, float] = field(default_factory=dict)
    #: The per-stage histogram *under this point*. This is what makes a trial
    #: explainable: a point that improved recall by moving 12 queries out of
    #: `fusion` and into `served` is a different (and much more trustworthy)
    #: result than one that improved recall while shuffling every stage.
    histogram: dict[str, Any] = field(default_factory=dict)
    #: Queries the trial could not evaluate, and why. Travels with the metrics
    #: because "improved 0.2 on the 40 queries it did not fail on" is a
    #: different claim from "improved 0.2".
    evaluated_queries: int = 0
    excluded_queries: int = 0
    exclusion_reasons: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0
    searches: int = 0
    created_at: str = field(default_factory=utc_now)
    failed: str = ""

    @classmethod
    def identity(cls, experiment_id: str, point_id: str, cohort_id: str) -> str:
        return "trial-" + digest(experiment_id, point_id, cohort_id)

    def validate(self) -> list[str]:
        """Reasons this trial may not be stored, or an empty list."""

        problems: list[str] = []
        if self.failed:
            return problems
        if not self.metrics:
            problems.append("no metrics")
        if not self.evaluated_queries:
            problems.append("no denominator: zero queries evaluated")
        if not self.proposal.motivating_stage:
            problems.append("proposal names no motivating stage")
        if self.proposal.point.parent_point_id and not self.proposal.point.delta:
            problems.append("a non-baseline point declares no parameter delta")
        return problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "proposal": self.proposal.as_dict(),
            "cohort_id": self.cohort_id,
            "cohort_name": self.cohort_name,
            "metrics": dict(self.metrics),
            "histogram": dict(self.histogram),
            "evaluated_queries": self.evaluated_queries,
            "excluded_queries": self.excluded_queries,
            "exclusion_reasons": dict(self.exclusion_reasons),
            "duration_ms": round(self.duration_ms, 3),
            "searches": self.searches,
            "created_at": self.created_at,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class Comparison:
    """A trial against the baseline, paired by query id.

    Paired the same way the evaluation plane pairs its ablations, and for the
    same reason: a query that failed under one point and not the other must be
    excluded with a recorded reason rather than compared against a hole.
    """

    metric: str
    baseline_trial_id: str
    treatment_trial_id: str
    baseline_value: float
    treatment_value: float
    delta: float
    paired_queries: int
    improved_queries: int = 0
    regressed_queries: int = 0
    unchanged_queries: int = 0
    excluded_queries: int = 0
    exclusion_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def formula(self) -> str:
        return f"delta({self.metric}) = treatment - baseline"

    @property
    def substituted(self) -> str:
        return (
            f"delta({self.metric}) = {self.treatment_value:.6f} - {self.baseline_value:.6f}"
            f" = {self.delta:+.6f} over {self.paired_queries} paired queries"
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["formula"] = self.formula
        payload["substituted"] = self.substituted
        return payload


@dataclass(frozen=True)
class Decision:
    """What the experiment concluded, and every reason behind it.

    ``outcome`` is one of ``promote`` / ``reject`` / ``no_change`` /
    ``insufficient_evidence``. The last two are different on purpose:
    "the search found nothing better than the current configuration" is a
    result, and "there was not enough evidence to tell" is an absence of one.
    Collapsing them would let a region with no proof at all report that its
    parameters are optimal.
    """

    decision_id: str
    experiment_id: str
    outcome: str
    reason: str
    winning_point_id: str = ""
    comparisons: tuple[Comparison, ...] = ()
    gates: tuple[dict[str, Any], ...] = ()
    holdout_confirmed: bool = False
    control_regressed: bool = False
    created_at: str = field(default_factory=utc_now)

    @property
    def gates_passed(self) -> bool:
        """Every gate passed, and there was at least one gate.

        The empty-list guard is not defensive programming: the evaluation
        plane shipped without it, a skipped run carried no gates, ``all([])``
        answered ``True``, and a batch that never ran reported that its gates
        passed -- straight into a CLI exit status.
        """

        return bool(self.gates) and all(bool(gate.get("passed")) for gate in self.gates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "experiment_id": self.experiment_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "winning_point_id": self.winning_point_id,
            "comparisons": [item.as_dict() for item in self.comparisons],
            "gates": [dict(gate) for gate in self.gates],
            "gates_passed": self.gates_passed,
            "holdout_confirmed": self.holdout_confirmed,
            "control_regressed": self.control_regressed,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Experiment:
    """One tuning batch, addressed by what it is rather than when it ran."""

    experiment_id: str
    kb_id: str
    snapshot_id: str
    cohort_id: str
    holdout_cohort_id: str
    control_cohort_id: str
    space_digest: str
    budget: dict[str, int]
    baseline_point: ParameterPoint
    mode: str = "batch"
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def identity(
        cls,
        kb_id: str,
        snapshot_id: str,
        space_digest: str,
        cohort_id: str,
        budget: dict[str, int],
        baseline_point_id: str,
    ) -> str:
        return "exp-" + digest(
            kb_id,
            snapshot_id,
            space_digest,
            cohort_id,
            sorted(budget.items()),
            baseline_point_id,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["baseline_point"] = self.baseline_point.as_dict()
        return payload


@dataclass(frozen=True)
class TuningBundle:
    """A packaged configuration set: the deliverable of the whole plane.

    A bundle is deliberately more than a dict of numbers. It carries the
    snapshot it was measured against, the decision that produced it, the
    metrics that justify it, and the parameters it *replaces* -- the last of
    which is what makes rollback a stored fact rather than an operator's
    memory of what the config used to say.

    It is also portable. The digest is over the parameter values alone, so two
    regions that arrived at the same configuration produce the same bundle id
    and an operator can tell at a glance that a fleet is converged.
    """

    bundle_id: str
    kb_id: str
    experiment_id: str
    decision_id: str
    snapshot_id: str
    parameters: dict[str, float]
    #: What was in force when this bundle was created. Rollback restores it.
    replaces: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    comparisons: tuple[dict[str, Any], ...] = ()
    gates: tuple[dict[str, Any], ...] = ()
    diagnosis_id: str = ""
    motivating_stage: str = ""
    rationale: str = ""
    created_at: str = field(default_factory=utc_now)
    #: Set when applied; empty while the bundle is only a proposal. A bundle
    #: is a *file* until something applies it, which is what makes producing
    #: one safe to do automatically and applying one a separate decision.
    applied_at: str = ""
    applied_by: str = ""

    @classmethod
    def identity(cls, parameters: dict[str, float]) -> str:
        return "bundle-" + digest(sorted((k, float(v)) for k, v in parameters.items()))

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["comparisons"] = [dict(item) for item in self.comparisons]
        payload["gates"] = [dict(item) for item in self.gates]
        return payload
