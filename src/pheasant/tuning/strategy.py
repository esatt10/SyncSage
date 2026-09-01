"""Which points to try, in what order, and when to stop.

The search is deliberately small and deliberately explicable. It is not a
Bayesian optimizer, and the reason is the same one that makes this plane
useful at all: a cohort here is tens to hundreds of queries, and the effect
sizes are small. A method with enough freedom to fit that data *will* fit it,
and produce a parameter set that is a description of the cohort rather than of
the corpus. So the search is restricted along three axes at once, and each
restriction is a defence against a specific way of fooling yourself.

**Restricted by diagnosis.** Only parameters whose stage the diagnosis blames
are proposed. If every miss is in the lexical arm, the fusion constant is not
on the table -- not because it could not produce a better number on this
cohort, but because any number it produces is noise, and a search that explores
it will eventually find some.

**Restricted to one coordinate at a time.** Coordinate descent, single steps
along each ladder. A joint search over twelve parameters has enough
combinations to find an apparent winner in pure noise; a one-parameter change
that improves a cohort is a claim small enough to check, and the resulting
bundle is small enough for a person to read and disagree with.

**Restricted by budget, spent cheap-first.** The fusion family costs nothing
per trial (re-fusion), so its ladders are walked fully. The candidate-generation
family costs a full replay per trial, so it gets a separate, much smaller
budget, and successive halving spends it on the survivors: every point gets a
first pass on a small slice of the cohort, and only the top half of each round
is carried forward. Most bad points are eliminated after seeing a fraction of
the queries.

**And it can decline.** :func:`propose` returns an empty list when the
diagnosis says the failures are not in a stage any parameter moves. A tuning
pass that reports "the misses are documents that were never indexed; no
retrieval parameter will help" has done its job. The alternative -- searching
anyway and shipping whatever came out highest -- is the failure mode this whole
package is arranged to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pheasant.tuning.contracts import ParameterPoint, Proposal
from pheasant.tuning.space import REFUSION, REQUERY, Parameter, ParameterSpace

logger = logging.getLogger(__name__)

#: Below this share of actionable misses, tuning is the wrong tool and the
#: strategy says so instead of searching. Two thirds: with a third of the
#: misses outside anything a parameter can reach, even a perfect parameter set
#: leaves the dominant complaint untouched, and shipping one would misdirect
#: whoever reads the report.
MIN_ACTIONABLE_SHARE = 0.34

#: Successive-halving rounds for the expensive family, and the fraction of the
#: cohort the first round sees.
HALVING_ROUNDS = 3
FIRST_ROUND_SHARE = 0.34


@dataclass(frozen=True)
class Budget:
    """What one experiment may spend.

    Two separate numbers rather than one, because the two cost classes differ
    by three orders of magnitude and a single "trials" budget would be spent
    entirely on whichever class the enumeration happened to reach first.
    """

    #: Points evaluated by re-fusion. Effectively free: they cost arithmetic
    #: over lists already in memory.
    refusion_trials: int = 400
    #: Points that need a real retrieval per query. This is the number that
    #: decides how long a batch runs.
    requery_trials: int = 24
    #: Hard ceiling on searches, whatever the trial counts imply. The backstop
    #: that keeps a large cohort from turning a modest trial budget into the
    #: region's dominant workload.
    max_searches: int = 5000

    def as_dict(self) -> dict[str, int]:
        return {
            "refusion_trials": self.refusion_trials,
            "requery_trials": self.requery_trials,
            "max_searches": self.max_searches,
        }


def stages_to_explore(histogram: dict[str, Any], *, limit: int = 3) -> list[str]:
    """The blamed stages, most misses first, actionable ones only.

    Capped: a cohort with misses spread evenly over every stage has no dominant
    failure, and searching all of them at once is how a budget gets spent
    proving that everything helps a little.
    """

    from pheasant.tuning.stages import ACTIONABLE_STAGES

    ranked = [
        entry["stage"]
        for entry in histogram.get("ranked") or []
        if entry["stage"] in ACTIONABLE_STAGES
    ]
    return ranked[:limit]


def propose(
    space: ParameterSpace,
    baseline: dict[str, float],
    histogram: dict[str, Any],
    *,
    budget: Budget | None = None,
    generation: int = 0,
    current: dict[str, float] | None = None,
) -> list[Proposal]:
    """One generation of candidate points, cheap class first.

    ``current`` is the incumbent the descent is stepping away from -- the
    baseline in generation 0, the previous generation's winner afterwards.
    ``baseline`` never moves, because every proposal's delta is reported
    against the configuration the region is actually serving, not against a
    waypoint the search passed through.
    """

    budget = budget or Budget()
    current = dict(current or baseline)
    baseline_point = ParameterPoint.of(baseline)

    share = histogram.get("actionable_share")
    if share is not None and float(share) < MIN_ACTIONABLE_SHARE:
        logger.info(
            "tuning: %.0f%% of misses are outside any tunable stage; proposing nothing",
            float(share) * 100,
        )
        return []

    stages = stages_to_explore(histogram)
    if not stages:
        return []

    seen: set[str] = {ParameterPoint.of(current).point_id, baseline_point.point_id}
    proposals: list[Proposal] = []
    counts = {REFUSION: 0, REQUERY: 0}
    limits = {REFUSION: budget.refusion_trials, REQUERY: budget.requery_trials}

    # Cheap class first and in full: it costs nothing to walk, and a fusion
    # result also *informs* the expensive class — a corpus where re-weighting
    # the arms fixes half the misses needs fewer candidate-generation trials.
    for cost_class in (REFUSION, REQUERY):
        for stage in stages:
            for parameter in space.for_stage(stage):
                if parameter.cost_class != cost_class:
                    continue
                for value in parameter.neighbours(current.get(parameter.name, 0.0)):
                    if counts[cost_class] >= limits[cost_class]:
                        break
                    values = dict(current)
                    values[parameter.name] = value
                    point = ParameterPoint.of(values, parent=baseline_point)
                    if point.point_id in seen:
                        continue
                    seen.add(point.point_id)
                    counts[cost_class] += 1
                    proposals.append(
                        Proposal(
                            point=point,
                            motivating_stage=stage,
                            rationale=_rationale(parameter, stage, histogram, value, current),
                            cost_class=cost_class,
                            strategy="coordinate_descent",
                            generation=generation,
                        )
                    )
    return proposals


def _rationale(
    parameter: Parameter,
    stage: str,
    histogram: dict[str, Any],
    value: float,
    current: dict[str, float],
) -> str:
    """Why this point was tried, in a sentence a person can disagree with.

    Stored with the trial rather than rendered at read time: the reason a
    decision was made under has to survive a later change to this wording, and
    an audit trail that regenerates its own explanations is not one.
    """

    misses = int(histogram.get("counts", {}).get(stage, 0))
    total = int(histogram.get("misses", 0)) or 1
    was = current.get(parameter.name)
    return (
        f"{misses} of {total} misses ({misses / total:.0%}) were attributed to {stage}; "
        f"{parameter.name} acts on that stage. Trying {value:g} in place of "
        f"{was:g}. {parameter.rationale}"
        if was is not None
        else f"{misses} of {total} misses were attributed to {stage}; trying "
        f"{parameter.name}={value:g}. {parameter.rationale}"
    )


def halving_schedule(
    query_ids: list[str],
    trial_count: int,
    *,
    rounds: int = HALVING_ROUNDS,
    first_share: float = FIRST_ROUND_SHARE,
) -> list[tuple[int, int]]:
    """``[(queries this round, points carried into it), ...]``.

    Successive halving, stated as a plan rather than discovered mid-run, so a
    batch can report what it is about to spend before it spends it -- and so a
    resumed batch replays the same schedule rather than re-deriving one from
    however much it happens to have finished.

    Degrades to a single full round when there is nothing to halve: with three
    points and forty queries, the bookkeeping costs more than the saving.
    """

    total = len(query_ids)
    if trial_count <= 2 or total < 6 or rounds <= 1:
        return [(total, trial_count)]
    schedule: list[tuple[int, int]] = []
    queries = max(1, int(total * first_share))
    carried = trial_count
    for index in range(rounds):
        last = index == rounds - 1
        schedule.append((total if last else min(total, queries), max(1, carried)))
        queries = min(total, queries * 2)
        carried = max(1, carried // 2)
    return schedule


def select_survivors(
    scored: list[tuple[str, float]],
    keep: int,
) -> list[str]:
    """The top ``keep`` point ids, ties broken deterministically.

    The tie break is the point id rather than insertion order on purpose: two
    replicas that enumerated proposals in different orders must carry the same
    survivors forward, or a resumed batch computes different numbers from an
    uninterrupted one.
    """

    ordered = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
    return [point_id for point_id, _ in ordered[: max(1, keep)]]
