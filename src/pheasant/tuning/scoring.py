"""Scoring a ranked list against known positives.

Small on purpose. The evaluation plane's :mod:`pheasant.evaluation.metrics` is
the right tool for *publishing* a measurement -- every result there carries a
formula, a substituted calculation, operands, proof ids and a limitation,
because it is a claim about the region that somebody will read. A tuning trial
is not that. It is an internal comparison between two orderings of the same
queries under the same evidence, run four hundred times, and dressing each one
as a published metric would produce four hundred audit documents nobody reads
and make the cheap path expensive.

So the numbers here are deliberately plain, and the discipline is moved to the
two places it matters:

* the **evidence** comes from the evaluation plane's projection unchanged, so a
  trial is scored against the same positives a published metric would use --
  never against a rank the region chose to show, which would measure the
  region's confidence rather than its correctness;
* the **decision** is a published artifact, and :class:`Comparison` carries the
  formula, the substitution, the paired denominator and the exclusions.

Reciprocal rank is the primary metric. It is the one that distinguishes the two
outcomes a fusion parameter actually trades between -- "found at rank 1" and
"found at rank 9" are both a hit for recall@10 and are not the same answer --
and it is bounded, which keeps a single query with an enormous rank from
dominating a cohort mean.

Queries with **no** known positive are excluded from every denominator rather
than counted as misses. Counting them would make a trial's score depend on how
much of the cohort happens to be evidenced, and the search would then prefer
parameters that do well on the evidenced fraction of an arbitrary sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The k values every trial reports. Two, not one: a point that lifts recall@10
#: while dropping recall@5 has moved documents *into* the list and *down* it,
#: which is a trade a decision should be able to see.
K_VALUES: tuple[int, ...] = (5, 10)


@dataclass
class QueryScore:
    """One query's outcome under one parameter point."""

    query_id: str
    reciprocal_rank: float = 0.0
    first_rank: int | None = None
    recall: dict[int, float] = field(default_factory=dict)
    positives: int = 0
    returned: int = 0


def score_query(ranked_ids: list[str], positives: list[str]) -> QueryScore | None:
    """Score one ranked list, or ``None`` when there is nothing to score.

    ``None`` rather than a zero: a query with no known positive is not a
    failure, it is an absence of evidence, and the difference is the whole
    reason this plane keeps a separate ``unevidenced`` count.
    """

    wanted = set(positives)
    if not wanted:
        return None
    score = QueryScore(query_id="", positives=len(wanted), returned=len(ranked_ids))
    for index, node_id in enumerate(ranked_ids, start=1):
        if node_id in wanted:
            score.first_rank = index
            score.reciprocal_rank = 1.0 / index
            break
    for k in K_VALUES:
        hits = wanted & set(ranked_ids[:k])
        score.recall[k] = len(hits) / len(wanted)
    return score


@dataclass
class CohortScore:
    """A whole cohort under one point: the aggregates and the rows behind them."""

    per_query: dict[str, QueryScore] = field(default_factory=dict)
    unevidenced: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def evaluated(self) -> int:
        return len(self.per_query)

    def aggregate(self) -> dict[str, float]:
        """Means over the evaluated queries. Empty when there are none.

        Empty rather than zeroed: a cohort with no evidence has no score, and
        emitting ``0.0`` would let a point with no evidence at all compare
        equal to one that was measured and did badly.
        """

        if not self.per_query:
            return {}
        n = len(self.per_query)
        out = {
            "known_positive_reciprocal_rank": sum(
                s.reciprocal_rank for s in self.per_query.values()
            )
            / n
        }
        for k in K_VALUES:
            out[f"known_positive_recall_at_{k}"] = (
                sum(s.recall.get(k, 0.0) for s in self.per_query.values()) / n
            )
        out["known_positive_hit_rate"] = (
            sum(1 for s in self.per_query.values() if s.first_rank is not None) / n
        )
        return out


def score_cohort(
    rankings: dict[str, list[str]],
    evidence: dict[str, list[str]],
    *,
    failures: dict[str, str] | None = None,
) -> CohortScore:
    """Score every query in a cohort under one point."""

    result = CohortScore(failed=dict(failures or {}))
    for query_id, ranked in rankings.items():
        if query_id in result.failed:
            continue
        scored = score_query(ranked, evidence.get(query_id) or [])
        if scored is None:
            result.unevidenced += 1
            continue
        scored.query_id = query_id
        result.per_query[query_id] = scored
    return result


def compare(
    metric: str,
    baseline: CohortScore,
    treatment: CohortScore,
) -> dict[str, Any]:
    """Paired comparison of two cohort scores on one metric.

    **Paired by query id, never by position.** A query that failed under one
    point and not the other is excluded with a recorded reason rather than
    compared against a hole -- the same rule the evaluation plane's ablations
    follow, and for the same reason: a treatment that improved by 0.2 on the
    forty queries it did not fail on is a different claim from one that
    improved by 0.2.
    """

    both = sorted(set(baseline.per_query) & set(treatment.per_query))
    reasons: dict[str, int] = {}
    for query_id in set(baseline.per_query) | set(treatment.per_query):
        if query_id in both:
            continue
        in_baseline = query_id in baseline.per_query
        reasons["missing_from_treatment" if in_baseline else "missing_from_baseline"] = (
            reasons.get("missing_from_treatment" if in_baseline else "missing_from_baseline", 0) + 1
        )

    def value(score: QueryScore) -> float:
        if metric == "known_positive_reciprocal_rank":
            return score.reciprocal_rank
        if metric.startswith("known_positive_recall_at_"):
            return score.recall.get(int(metric.rsplit("_", 1)[1]), 0.0)
        if metric == "known_positive_hit_rate":
            return 1.0 if score.first_rank is not None else 0.0
        return 0.0

    improved = regressed = unchanged = 0
    base_sum = treat_sum = 0.0
    for query_id in both:
        before = value(baseline.per_query[query_id])
        after = value(treatment.per_query[query_id])
        base_sum += before
        treat_sum += after
        if after > before:
            improved += 1
        elif after < before:
            regressed += 1
        else:
            unchanged += 1

    n = len(both)
    base_mean = base_sum / n if n else 0.0
    treat_mean = treat_sum / n if n else 0.0
    return {
        "metric": metric,
        "baseline_value": base_mean,
        "treatment_value": treat_mean,
        "delta": treat_mean - base_mean,
        "paired_queries": n,
        "improved_queries": improved,
        "regressed_queries": regressed,
        "unchanged_queries": unchanged,
        "excluded_queries": sum(reasons.values()),
        "exclusion_reasons": reasons,
    }
