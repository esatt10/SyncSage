"""Packaging a configuration set, and the two acts of applying it.

A bundle is the deliverable. It is not a dict of numbers: it carries the
snapshot it was measured against, the decision that produced it, the
comparisons and gates behind that decision, the stage it was meant to fix, and
the parameters it replaces. Reading one should answer "why does this region
rank the way it does" without opening anything else.

**Producing and applying are separate, deliberately.** ``package`` writes a
bundle as a *proposal*; ``apply`` makes it the region's overlay. A batch may do
the first unattended -- it is a file describing a configuration, and creating
one changes nothing. The second changes what every replica serves and needs
somebody, or an explicit ``auto_apply``, to say so. Collapsing them would mean
a scheduled batch could silently re-rank a production region overnight on the
strength of a cohort nobody reviewed.

**Rollback is a stored fact.** ``replaces`` holds the parameters that were in
force at the moment of application, so reverting does not depend on anyone
remembering what the config file used to say -- or on the config file still
saying it.

**The digest is over the parameters alone.** Two regions that arrive at the
same configuration produce the same ``bundle_id``, so ``pheasant tune status``
across a fleet shows at a glance whether it is converged. Provenance differing
while parameters agree is exactly the case where the ids *should* match.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.search.ranking import PARAMETER_STAGES, clamp
from pheasant.tuning.contracts import Comparison, Decision, Diagnosis, Experiment, TuningBundle

logger = logging.getLogger(__name__)


def package(
    experiment: Experiment,
    decision: Decision,
    *,
    parameters: dict[str, float],
    baseline: dict[str, float],
    metrics: dict[str, float],
    comparisons: tuple[Comparison, ...] = (),
    diagnosis: Diagnosis | None = None,
    motivating_stage: str = "",
) -> TuningBundle:
    """Build the bundle for a decision. Writes nothing.

    Parameters are clamped on the way in, through the same rule a hand-edited
    config takes. A bundle that could not be served is not a bundle -- and the
    place to find that out is here, not when a replica reads the overlay and
    silently falls back to its configured values.
    """

    clamped = {
        name: clamp(name, float(value))
        for name, value in parameters.items()
        if name in PARAMETER_STAGES
    }
    dropped = sorted(set(parameters) - set(clamped))
    if dropped:
        logger.warning("tuning: bundle drops unknown parameters %s", dropped)
    return TuningBundle(
        bundle_id=TuningBundle.identity(clamped),
        kb_id=experiment.kb_id,
        experiment_id=experiment.experiment_id,
        decision_id=decision.decision_id,
        snapshot_id=experiment.snapshot_id,
        parameters=clamped,
        replaces=dict(baseline),
        metrics=dict(metrics),
        comparisons=tuple(item.as_dict() for item in comparisons),
        gates=tuple(dict(gate) for gate in decision.gates),
        diagnosis_id=diagnosis.diagnosis_id if diagnosis else "",
        motivating_stage=motivating_stage,
        rationale=decision.reason,
    )


def as_config_fragment(bundle: TuningBundle | dict[str, Any]) -> dict[str, Any]:
    """The bundle as the ``search.ranking`` block it is equivalent to.

    What an operator pastes into ``pheasant.yaml`` to make a bundle permanent
    rather than an applied overlay. Offered because the overlay lives in
    ``/state`` and ``/state`` is not what a team reviews in a pull request: a
    tuning result that cannot be moved into version control is a tuning result
    that will be lost the next time somebody resets a volume.
    """

    if isinstance(bundle, TuningBundle):
        parameters = bundle.parameters
    else:
        # Accepts either a bundle payload (`parameters`) or the
        # `active_parameters` shape (`values`), because both are "the set of
        # numbers this region would rank with" and a caller holding one should
        # not have to reshape it into the other to render it.
        parameters = bundle.get("parameters") or bundle.get("values") or {}
    return {
        "search": {"ranking": {name: float(value) for name, value in sorted(parameters.items())}}
    }


def diff(bundle: TuningBundle | dict[str, Any], current: dict[str, float]) -> list[dict[str, Any]]:
    """What applying this bundle would change, parameter by parameter.

    The thing to show a person before they apply something. Reports the stage
    each change acts on, because "rrf_k: 60 -> 30" means nothing on its own and
    "rrf_k: 60 -> 30 (fusion)" means the experiment thought fusion was the
    problem.
    """

    parameters = (
        bundle.parameters if isinstance(bundle, TuningBundle) else bundle.get("parameters") or {}
    )
    changes: list[dict[str, Any]] = []
    for name in sorted(parameters):
        before = current.get(name)
        after = float(parameters[name])
        if before is not None and float(before) == after:
            continue
        changes.append(
            {
                "parameter": name,
                "stage": PARAMETER_STAGES.get(name, "unknown"),
                "from": before,
                "to": after,
            }
        )
    return changes
