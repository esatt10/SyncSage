"""What "better" means, stated once and carried everywhere.

A tuning batch ranks trials, picks a winner and gates it. Every one of those
steps needs an answer to "better by what?", and until this module existed the
answer was a constant: reciprocal rank, chosen because it is the metric that
distinguishes the two outcomes a fusion parameter actually trades between.

That is a good default and a bad *only* option, because it encodes a product
decision that is not ours to make. A region whose agents read one result cares
about rank one and should optimize reciprocal rank. A region whose agents read
the whole top-10 and synthesize cares about recall@10 and is *harmed* by a
parameter set that sharpens rank one while dropping a document out of the list
entirely. Both are legitimate; they are different objectives; and a plane that
silently assumed the first would quietly make the second region worse while
reporting an improvement.

So the objective is named, configured, explained, and threaded through:

* the strategy ranks trials by it,
* the decision compares against the baseline on it,
* the gates read it,
* the report and every surface publish which one ran,
* and MLflow logs it as the primary metric.

**Every objective states what it trades away.** That is the field that matters
most here. An objective is a choice to care about one thing more than another,
and a UI that showed "objective: recall_at_10" without "this will accept a
worse rank-1 in exchange for a document appearing at all" has told the reader
the name of a decision without its content.

**A weighted objective is normalized, never summed raw.** Reciprocal rank and
recall are both 0-1 here, so a naive sum happens to work — and would silently
stop working the moment somebody adds a metric on another scale. Weights are
normalized to sum to one, and the substitution is published, so a composite
score can be argued with rather than merely read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The metric a trial is ranked by when nothing says otherwise. Named here
#: rather than in the store so there is one home for "what better means".
DEFAULT_OBJECTIVE = "reciprocal_rank"


@dataclass(frozen=True)
class Objective:
    """One definition of "better", with its trade stated.

    ``weights`` maps metric id to weight. A single-metric objective is just a
    weight of 1.0 on one metric, which keeps the scoring path identical for
    both and means the composite case is not a second, less-tested branch.
    """

    objective_id: str
    label: str
    weights: dict[str, float]
    summary: str
    #: What optimizing this will accept getting worse. Required, not optional:
    #: an objective without a stated trade is a preference presented as an
    #: optimum.
    trades_away: str
    higher_is_better: bool = True

    @property
    def primary_metric(self) -> str:
        """The single metric this reduces to, or "" for a composite.

        Reported so a reader can tell at a glance whether they are looking at
        a measurement or an aggregate of several.
        """

        return next(iter(self.weights)) if len(self.weights) == 1 else ""

    def normalized(self) -> dict[str, float]:
        total = sum(abs(w) for w in self.weights.values()) or 1.0
        return {metric: weight / total for metric, weight in self.weights.items()}

    def score(self, metrics: dict[str, float]) -> float | None:
        """This objective's value over one trial's metrics, or ``None``.

        ``None`` when a component is missing, rather than treating it as zero.
        A composite that silently scored a missing metric as 0.0 would rank a
        point that *could not be measured* below one that measured badly, and
        those are different things.
        """

        weights = self.normalized()
        total = 0.0
        for metric, weight in weights.items():
            value = metrics.get(metric)
            if value is None:
                return None
            total += weight * float(value)
        return total

    def substituted(self, metrics: dict[str, float]) -> str:
        """The arithmetic, written out. What makes a composite arguable."""

        weights = self.normalized()
        if len(weights) == 1:
            metric = next(iter(weights))
            value = metrics.get(metric)
            return f"{metric} = {value if value is None else round(float(value), 6)}"
        parts = [
            f"{weight:.3g}x{metric}({metrics.get(metric, 'missing')})"
            for metric, weight in sorted(weights.items())
        ]
        score = self.score(metrics)
        return " + ".join(parts) + f" = {score if score is None else round(score, 6)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "label": self.label,
            "weights": self.normalized(),
            "primary_metric": self.primary_metric,
            "summary": self.summary,
            "trades_away": self.trades_away,
            "higher_is_better": self.higher_is_better,
        }


#: The objectives a region can pick by name.
#:
#: Deliberately few. A menu of twenty invites picking one by its name rather
#: than by what it does, and every entry here is a genuinely different product
#: decision rather than a variation on one.
BUILTIN: dict[str, Objective] = {
    "reciprocal_rank": Objective(
        objective_id="reciprocal_rank",
        label="Rank of the first good result",
        weights={"known_positive_reciprocal_rank": 1.0},
        summary=(
            "Optimizes how high the first known-good document lands. 1.0 means "
            "rank one; 0.5 means rank two. The default, because it is the "
            "metric that separates the two outcomes a fusion parameter "
            "actually trades between — 'found at rank 1' and 'found at rank 9' "
            "are both a hit for recall and are not the same answer."
        ),
        trades_away=(
            "Will accept a document dropping out of the list entirely in "
            "exchange for a sharper top result. Wrong for a region whose "
            "agents read the whole page and synthesize across it."
        ),
    ),
    "recall_at_5": Objective(
        objective_id="recall_at_5",
        label="Good documents in the top 5",
        weights={"known_positive_recall_at_5": 1.0},
        summary=(
            "Optimizes how many known-good documents appear in the first five "
            "results, regardless of their order within those five. Right when "
            "something downstream reads several results and re-ranks or "
            "synthesizes them."
        ),
        trades_away=(
            "Indifferent to order inside the top five, so it will happily move "
            "the best answer from rank 1 to rank 5 to fit another one in. "
            "Wrong for a caller that reads only the first result."
        ),
    ),
    "recall_at_10": Objective(
        objective_id="recall_at_10",
        label="Good documents in the top 10",
        weights={"known_positive_recall_at_10": 1.0},
        summary=(
            "The same trade as recall_at_5 over a wider window. Right for "
            "agentic callers that fetch a page of context and let a model "
            "decide what matters."
        ),
        trades_away=(
            "Order-blind across ten results, so it optimizes for a document "
            "being *present* rather than being *found*. A human reading a "
            "result list top-down will not feel this improve."
        ),
    ),
    "hit_rate": Objective(
        objective_id="hit_rate",
        label="Queries that found anything good at all",
        weights={"known_positive_hit_rate": 1.0},
        summary=(
            "The share of queries where a known-good document appeared "
            "anywhere in the results. The bluntest objective, and the right "
            "one when the complaint is 'it finds nothing' rather than 'the "
            "ordering is wrong'."
        ),
        trades_away=(
            "Says nothing about position, so a parameter set that moves every "
            "answer from rank 1 to rank 10 scores identically. Use it to get "
            "off the floor, then switch."
        ),
    ),
    "balanced": Objective(
        objective_id="balanced",
        label="Rank and coverage together",
        weights={
            "known_positive_reciprocal_rank": 0.5,
            "known_positive_recall_at_10": 0.5,
        },
        summary=(
            "Half rank, half coverage. Refuses parameter sets that buy a "
            "sharper first result by dropping documents out of the list, and "
            "equally refuses ones that pack the list at the cost of the top "
            "answer."
        ),
        trades_away=(
            "Optimizes neither cleanly. A composite is harder to reason about "
            "than either component, and a movement in it always needs the "
            "components read alongside — which is why the report publishes "
            "both."
        ),
    ),
}


def resolve(settings: Any) -> Objective:
    """The objective this region tunes for.

    Falls back to the default rather than raising on an unknown name: a typo
    in a config file should cost an operator the objective they meant, not
    every tuning batch until somebody notices. The resolved objective is
    published on every report, so the fallback is visible rather than silent.

    A custom weight map wins over a name, because a caller who wrote weights
    has been more specific than one who picked a label.
    """

    if settings is None:
        return BUILTIN[DEFAULT_OBJECTIVE]
    weights = dict(getattr(settings, "weights", None) or {})
    if weights:
        return Objective(
            objective_id="custom",
            label="Custom weighting",
            weights={str(k): float(v) for k, v in weights.items()},
            summary=(
                "A weighted combination this region defined: "
                + ", ".join(f"{k} x{v:g}" for k, v in sorted(weights.items()))
                + ". Weights are normalized to sum to one, and the "
                "substituted arithmetic travels with every comparison."
            ),
            trades_away=(
                "Whatever the components do not measure. A custom objective is "
                "only as good as the metrics in it, and nothing here checks "
                "that the combination means something."
            ),
            higher_is_better=bool(getattr(settings, "higher_is_better", True)),
        )
    name = str(getattr(settings, "metric", "") or DEFAULT_OBJECTIVE)
    return BUILTIN.get(name, BUILTIN[DEFAULT_OBJECTIVE])


def catalog() -> list[dict[str, Any]]:
    """Every built-in objective, for a surface that offers a choice."""

    return [objective.as_dict() for objective in BUILTIN.values()]
