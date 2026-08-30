"""The metric registry: numbers that carry their own denominator and evidence.

Every function here returns a :class:`~pheasant.evaluation.contracts.MetricResult`,
and a result that cannot state its formula, its substituted calculation, its
denominator and its one limitation does not get published -- ``validate()``
rejects it before it is persisted. That is deliberate friction. A bare score
called "knowledge-base accuracy" is the artifact this whole plane exists to
avoid producing, and the cheapest way not to produce one is to make it
structurally awkward to express.

Four rules the implementations follow without exception.

**A metric names what it measured, in its own name.** ``known_positive_recall``
is recall over artifacts with positive *proof*, not over the corpus. There is
no way to measure exhaustive corpus recall without exhaustive judgments, and a
metric named ``recall`` that quietly means "of the five things we happen to
know about" will be read as the former by everybody.

**Unjudged is never negative.** Precision-shaped metrics count only judged
items, and ``result_evidence_coverage`` is published beside them so a reader
can see how much of the result list the judgment covered. A 0.9 over 10% of
the list and a 0.9 over 90% of it are different findings.

**A missing input yields ``insufficient_evidence``, never 0.0.** A cohort with
no positive proof has an undefined recall; reporting zero would put a red bar on
a dashboard describing an instrumentation gap.

**Deltas are paired by query id.** Every attribution metric subtracts a
treatment from its own baseline over the queries *both* completed, and carries
the excluded count and reasons with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pheasant.evaluation.contracts import (
    Classification,
    Cohort,
    MetricResult,
    MetricScope,
    MetricStatus,
)
from pheasant.evaluation.proof import ProofPolicy, QueryEvidence, conflict_rate
from pheasant.evaluation.replay import VariantReplay, paired_ids

#: Rank assigned to an item a run never returned, used by pairwise comparisons.
#: Applied consistently to both sides of a pair, so it can shift an absolute
#: value but never the sign of a difference.
TERMINAL_RANK = 10_000


@dataclass
class MetricContext:
    """Everything a metric needs that is not the run it is measuring."""

    snapshot_id: str
    cohort: Cohort
    policy: ProofPolicy
    evidence: dict[str, QueryEvidence]
    #: Per-query results are the audit trail an aggregate resolves to. Bounded
    #: so a 5,000-query rolling cohort cannot turn one run's report into a
    #: document nothing can open.
    max_per_query_results: int = 200

    @property
    def floor(self) -> float:
        return self.policy.positive_floor

    def positives(self, query_id: str) -> list[str]:
        ev = self.evidence.get(query_id)
        return ev.positives(self.floor) if ev else []

    def negatives(self, query_id: str) -> list[str]:
        ev = self.evidence.get(query_id)
        return ev.negatives(self.floor) if ev else []

    def judged(self, query_id: str) -> set[str]:
        ev = self.evidence.get(query_id)
        return ev.judged(self.floor) if ev else set()

    def utility(self, query_id: str, target_id: str) -> float:
        ev = self.evidence.get(query_id)
        return ev.utility(target_id) if ev else 0.0

    def proof_ids(self, query_id: str) -> list[str]:
        ev = self.evidence.get(query_id)
        return ev.proof_ids() if ev else []


@dataclass
class MetricSet:
    """Aggregates and the per-query rows behind them."""

    aggregates: list[MetricResult] = field(default_factory=list)
    per_query: list[MetricResult] = field(default_factory=list)

    def extend(self, other: MetricSet) -> None:
        self.aggregates.extend(other.aggregates)
        self.per_query.extend(other.per_query)

    def all(self) -> list[MetricResult]:
        return [*self.aggregates, *self.per_query]

    def by_id(self, metric_id: str, variant_id: str | None = None) -> MetricResult | None:
        for result in self.aggregates:
            if result.metric_id == metric_id and (
                variant_id is None or result.scope.variant_id == variant_id
            ):
                return result
        return None


def _scope(ctx: MetricContext, variant_id: str | None, query_id: str | None = None) -> MetricScope:
    return MetricScope(
        snapshot_id=ctx.snapshot_id,
        cohort_id=ctx.cohort.cohort_id,
        variant_id=variant_id,
        query_id=query_id,
    )


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _insufficient(
    ctx: MetricContext,
    metric_id: str,
    variant_id: str | None,
    *,
    formula: str,
    reason: str,
    classification: str = Classification.DEMONSTRATED.value,
    denominator: float | None = 0,
) -> MetricResult:
    """The honest answer when the inputs are not there.

    Note the ``value=None``. A caller charting this gets a gap rather than a
    zero, which is the difference between "we could not measure" and "we
    measured badly" -- and dashboards that cannot show the first one train
    people to ignore the second.
    """

    return MetricResult(
        metric_id=metric_id,
        classification=classification,
        scope=_scope(ctx, variant_id),
        value=None,
        formula=formula,
        substituted="not computed",
        denominator=denominator,
        status=MetricStatus.INSUFFICIENT_EVIDENCE.value,
        summary=reason,
        supports_claim="nothing; there was not enough evidence to compute this",
        does_not_support=reason,
    )


# --------------------------------------------------------------------------
# 12. evidence coverage


def query_evidence_coverage(ctx: MetricContext) -> MetricResult:
    """|Q_E| / |Q| -- the share of the cohort that could support a judgment.

    Published beside every demonstrated metric, and it is the number that keeps
    the rest honest: a recall of 0.89 over 44% coverage is a claim about 44% of
    the cohort, and reporting it without this makes it look like a claim about
    all of it.
    """

    eligible = list(ctx.cohort.query_ids)
    evidenced = [qid for qid in eligible if ctx.evidence.get(qid, None) and ctx.judged(qid)]
    value = round(len(evidenced) / len(eligible), 6) if eligible else None
    return MetricResult(
        metric_id="query_evidence_coverage",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, None),
        value=value,
        formula="|Q_E| / |Q|",
        substituted=f"{len(evidenced)} / {len(eligible)}",
        numerator=len(evidenced),
        denominator=len(eligible),
        status=(
            MetricStatus.INFORMATIONAL.value
            if eligible
            else MetricStatus.INSUFFICIENT_EVIDENCE.value
        ),
        operands={"evidenced_query_ids": evidenced[:50]},
        proof_ids=tuple(sorted({pid for qid in evidenced for pid in ctx.proof_ids(qid)})[:200]),
        summary=(
            f"{len(evidenced)} of {len(eligible)} evaluated queries had evidence capable of "
            "supporting a utility judgment."
        ),
        supports_claim="how much of this cohort's result is backed by interaction proof",
        does_not_support=(
            "the unevidenced remainder is not known to be bad -- it is unmeasured, and "
            "contributes only structural and diagnostic results"
        ),
    )


def result_evidence_coverage(ctx: MetricContext, replay: VariantReplay, k: int) -> MetricResult:
    """Judged share of what was actually returned, at k.

    Stops a high partial-judgment score from looking comprehensive. If 2 of 10
    returned items are judged, ``known_positive_recall`` is a statement about
    those 2 -- and this is the metric that says so out loud.
    """

    judged_total = 0
    returned_total = 0
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        top = result.top(k)
        returned_total += len(top)
        judged = ctx.judged(query_id)
        judged_total += sum(1 for item in top if item in judged)
    value = round(judged_total / returned_total, 6) if returned_total else None
    return MetricResult(
        metric_id=f"result_evidence_coverage_at_{k}",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="sum_q |R_{q,k} ∩ J_q| / sum_q |R_{q,k}|",
        substituted=f"{judged_total} / {returned_total}",
        numerator=judged_total,
        denominator=returned_total,
        status=(
            MetricStatus.INFORMATIONAL.value
            if returned_total
            else MetricStatus.INSUFFICIENT_EVIDENCE.value
        ),
        summary=(
            f"{judged_total} of {returned_total} results in the top {k} carried positive or "
            "negative proof."
        ),
        supports_claim="how much of the served result list the evidence actually covers",
        does_not_support=(
            "the unjudged remainder is not known to be irrelevant; it has never been judged"
        ),
    )


def proof_conflict_rate(ctx: MetricContext) -> MetricResult:
    """Targets carrying both positive and negative proof above the floor.

    Surfaced separately rather than averaged in. A target somebody accepted and
    somebody else rejected has a net near zero and is indistinguishable from an
    unknown one in any representation that stores only the net -- and it is
    usually the most informative row in the report, because it normally means
    the document is right for one reader and wrong for another.
    """

    conflicted, total = conflict_rate(ctx.evidence, ctx.policy)
    value = round(conflicted / total, 6) if total else None
    return MetricResult(
        metric_id="proof_conflict_rate",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, None),
        value=value,
        formula="|C| / |T|",
        substituted=f"{conflicted} / {total}",
        numerator=conflicted,
        denominator=total,
        status=(
            MetricStatus.INFORMATIONAL.value if total else MetricStatus.INSUFFICIENT_EVIDENCE.value
        ),
        summary=f"{conflicted} of {total} judged targets carry both positive and negative proof.",
        supports_claim="where readers disagree about the same artifact",
        does_not_support="which side of the disagreement is right",
    )


# --------------------------------------------------------------------------
# 13. deterministic system fidelity


def index_completeness(ctx: MetricContext, state: Any) -> list[MetricResult]:
    """Eligible artifacts represented per retrieval arm.

    Calculated per arm on purpose. "The index is complete" is four different
    claims -- lexical, vector, graph and memory -- and a corpus fully present
    in FTS and absent from the vector store is a specific, findable bug that
    one aggregate number hides.
    """

    def count(sql: str) -> int:
        try:
            rows = state.rows(sql)
            return int(rows[0]["c"]) if rows else 0
        except Exception:  # noqa: BLE001 - a missing arm is a zero, not a crash
            return 0

    eligible = count("SELECT COUNT(*) AS c FROM artifacts WHERE status='indexed'")
    if not eligible:
        eligible = count("SELECT COUNT(*) AS c FROM artifacts")
    lexical = count(
        "SELECT COUNT(DISTINCT artifact_id) AS c FROM chunks "
        "WHERE artifact_id IN (SELECT id FROM artifacts)"
    )
    memory_records = count("SELECT COUNT(*) AS c FROM memory_records")
    memory_indexed = count(
        "SELECT COUNT(*) AS c FROM memory_records m "
        "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.artifact_id = m.artifact_id)"
    )

    out: list[MetricResult] = []
    for arm, numerator, denominator, note in (
        ("lexical", lexical, eligible, "artifacts with at least one indexed chunk"),
        ("memory", memory_indexed, memory_records, "memory records whose file produced a chunk"),
    ):
        value = round(numerator / denominator, 6) if denominator else None
        out.append(
            MetricResult(
                metric_id=f"index_completeness_{arm}",
                classification=Classification.STRUCTURAL.value,
                scope=_scope(ctx, None),
                value=value,
                formula="represented / eligible",
                substituted=f"{numerator} / {denominator}",
                numerator=numerator,
                denominator=denominator,
                status=(
                    MetricStatus.PASS.value
                    if denominator and numerator == denominator
                    else MetricStatus.WARN.value
                    if denominator
                    else MetricStatus.NOT_APPLICABLE.value
                ),
                threshold=1.0,
                summary=f"{numerator} of {denominator} {note}.",
                supports_claim=f"the {arm} arm can see the corpus it is supposed to see",
                does_not_support="that what it returns for any query is relevant",
            )
        )
    return out


def rank_churn(
    ctx: MetricContext,
    replay: VariantReplay,
    previous: dict[str, list[str]],
    k: int,
) -> MetricResult:
    """How much the top-k moved since the previous snapshot.

    A change measure, not a quality one, and it is classified ``structural`` so
    nobody reads a rise as a regression. High churn after a re-index is
    expected; high churn after a config change nobody made is a finding.
    """

    overlaps: list[float] = []
    for query_id, result in replay.results.items():
        if result.failed or query_id not in previous:
            continue
        before = previous[query_id][:k]
        after = result.top(k)
        if not before and not after:
            continue
        denominator = max(len(before), len(after), 1)
        overlaps.append(1.0 - len(set(before) & set(after)) / denominator)
    value = _mean(overlaps)
    if value is None:
        return _insufficient(
            ctx,
            f"rank_churn_at_{k}",
            replay.variant.variant_id,
            formula="1 - |R_{t-1,k} ∩ R_{t,k}| / k",
            reason="no previous snapshot replay to compare against",
            classification=Classification.STRUCTURAL.value,
        )
    return MetricResult(
        metric_id=f"rank_churn_at_{k}",
        classification=Classification.STRUCTURAL.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="mean_q (1 - |R_{t-1,k} ∩ R_{t,k}| / k)",
        substituted=f"mean over {len(overlaps)} queries = {value}",
        numerator=round(value * len(overlaps), 6),
        denominator=len(overlaps),
        status=MetricStatus.INFORMATIONAL.value,
        summary=f"{value:.1%} of the top {k} changed since the previous snapshot, on average.",
        supports_claim="how much ranking moved between two states",
        does_not_support="whether the movement was an improvement",
    )


def arm_contribution(ctx: MetricContext, replay: VariantReplay, k: int) -> MetricResult:
    """Which arms put each result where it is.

    Reported as *presence*, not as an additive score share. Reciprocal rank
    fusion does not expose comparable per-arm components -- the arms score on
    incomparable scales, which is why RRF was chosen in the first place -- so a
    "contribution percentage" derived from them would be a number with no
    referent. Presence and agreement are what the fusion actually consumes.
    """

    counts: dict[str, int] = {}
    total = 0
    agreed = 0
    for result in replay.results.values():
        if result.failed:
            continue
        for node_id in result.top(k):
            arms = result.contributing_arms.get(node_id) or []
            total += 1
            if len(arms) > 1:
                agreed += 1
            for arm in arms:
                counts[arm] = counts.get(arm, 0) + 1
    value = round(agreed / total, 6) if total else None
    return MetricResult(
        metric_id=f"arm_agreement_at_{k}",
        classification=Classification.STRUCTURAL.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="results returned by more than one arm / results",
        substituted=f"{agreed} / {total}",
        numerator=agreed,
        denominator=total,
        status=(
            MetricStatus.INFORMATIONAL.value if total else MetricStatus.INSUFFICIENT_EVIDENCE.value
        ),
        operands={"per_arm_presence": dict(sorted(counts.items()))},
        summary=(
            f"{agreed} of {total} top-{k} results were returned by more than one arm; "
            f"presence by arm: {dict(sorted(counts.items()))}."
        ),
        supports_claim="how much the arms agree, which is what RRF promotes on",
        does_not_support=(
            "a per-arm share of the fused score -- RRF has no additive components to divide"
        ),
    )


# --------------------------------------------------------------------------
# 14. partial-proof retrieval effectiveness


def _per_query_result(
    ctx: MetricContext,
    metric_id: str,
    variant_id: str,
    query_id: str,
    *,
    value: float,
    formula: str,
    substituted: str,
    numerator: float,
    denominator: float,
    operands: dict[str, Any],
    summary: str,
    limitation: str,
) -> MetricResult:
    return MetricResult(
        metric_id=metric_id,
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, variant_id, query_id),
        value=value,
        formula=formula,
        substituted=substituted,
        numerator=numerator,
        denominator=denominator,
        status=MetricStatus.INFORMATIONAL.value,
        operands=operands,
        proof_ids=tuple(ctx.proof_ids(query_id)[:50]),
        summary=summary,
        supports_claim="this query's ranking of the artifacts it has proof about",
        does_not_support=limitation,
    )


def known_positive_recall(ctx: MetricContext, replay: VariantReplay, k: int) -> MetricSet:
    """|R_k ∩ P_q| / |P_q|, macro-averaged over queries with positive proof.

    Recall over *known* positives. Naming it anything shorter would invite the
    reading it cannot support: there is no way to know what fraction of all
    relevant corpus content was retrieved without judging all of it, and this
    metric does not pretend to.
    """

    metric_id = f"known_positive_recall_at_{k}"
    values: list[float] = []
    per_query: list[MetricResult] = []
    proof_ids: set[str] = set()
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        positives = ctx.positives(query_id)
        if not positives:
            continue
        top = result.top(k)
        hits = sorted(set(top) & set(positives))
        value = len(hits) / len(positives)
        values.append(value)
        proof_ids.update(ctx.proof_ids(query_id))
        if len(per_query) < ctx.max_per_query_results:
            per_query.append(
                _per_query_result(
                    ctx,
                    metric_id,
                    replay.variant.variant_id,
                    query_id,
                    value=round(value, 6),
                    formula="|R_k ∩ P_q| / |P_q|",
                    substituted=f"{len(hits)} / {len(positives)}",
                    numerator=len(hits),
                    denominator=len(positives),
                    operands={"returned_ids": top, "known_positive_ids": positives},
                    summary=(
                        f"{len(hits)} of {len(positives)} known-positive artifacts appeared in "
                        f"the top {k}."
                    ),
                    limitation=(
                        "unjudged results may also be relevant; this is not exhaustive corpus "
                        "recall"
                    ),
                )
            )

    if not values:
        return MetricSet(
            aggregates=[
                _insufficient(
                    ctx,
                    metric_id,
                    replay.variant.variant_id,
                    formula="|R_k ∩ P_q| / |P_q|",
                    reason="no query in this cohort has positive-proof artifacts",
                )
            ]
        )
    mean = _mean(values) or 0.0
    aggregate = MetricResult(
        metric_id=metric_id,
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=mean,
        formula="mean_q |R_k ∩ P_q| / |P_q|",
        substituted=f"mean over {len(values)} evidenced queries = {mean}",
        numerator=round(mean * len(values), 6),
        denominator=len(values),
        status=MetricStatus.INFORMATIONAL.value,
        proof_ids=tuple(sorted(proof_ids)[:200]),
        excluded_count=len(replay.results) - len(values),
        exclusion_reasons={"no_positive_proof": len(replay.results) - len(values)},
        summary=(
            f"On average {mean:.1%} of each query's known-positive artifacts were retrieved in "
            f"the top {k}, over {len(values)} evidenced queries."
        ),
        supports_claim="retrieval of artifacts previously demonstrated useful for these queries",
        does_not_support="exhaustive corpus recall, which no partial judgment can establish",
    )
    return MetricSet(aggregates=[aggregate], per_query=per_query)


def known_positive_hit(ctx: MetricContext, replay: VariantReplay, k: int) -> MetricResult:
    """The share of evidenced queries with at least one known positive in the top k."""

    hits = 0
    eligible = 0
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        positives = set(ctx.positives(query_id))
        if not positives:
            continue
        eligible += 1
        if positives & set(result.top(k)):
            hits += 1
    if not eligible:
        return _insufficient(
            ctx,
            f"known_positive_hit_at_{k}",
            replay.variant.variant_id,
            formula="queries with a positive in the top k / evidenced queries",
            reason="no query in this cohort has positive-proof artifacts",
        )
    value = round(hits / eligible, 6)
    return MetricResult(
        metric_id=f"known_positive_hit_at_{k}",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="|{q : R_{q,k} ∩ P_q ≠ ∅}| / |Q_E|",
        substituted=f"{hits} / {eligible}",
        numerator=hits,
        denominator=eligible,
        status=MetricStatus.INFORMATIONAL.value,
        summary=f"{hits} of {eligible} evidenced queries surfaced a known positive in the top {k}.",
        supports_claim="whether something previously useful was reachable at all",
        does_not_support="how well it was ranked, or whether anything else useful was missed",
    )


def known_positive_reciprocal_rank(ctx: MetricContext, replay: VariantReplay) -> MetricSet:
    """Mean 1/rank of the first known-positive artifact.

    The most sensitive of the ranking metrics to the thing memory is supposed
    to change: moving a known-good answer from rank three to rank one is a
    0.667 swing here and invisible to recall at five.
    """

    values: list[float] = []
    per_query: list[MetricResult] = []
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        positives = set(ctx.positives(query_id))
        if not positives:
            continue
        rank = next(
            (i for i, node_id in enumerate(result.ranked_ids, start=1) if node_id in positives),
            None,
        )
        value = 1.0 / rank if rank else 0.0
        values.append(value)
        if len(per_query) < ctx.max_per_query_results:
            per_query.append(
                _per_query_result(
                    ctx,
                    "known_positive_reciprocal_rank",
                    replay.variant.variant_id,
                    query_id,
                    value=round(value, 6),
                    formula="1 / rank(first positive)",
                    substituted=f"1 / {rank}" if rank else "0 (no positive returned)",
                    numerator=1 if rank else 0,
                    denominator=rank or 0,
                    operands={"first_positive_rank": rank, "known_positive_ids": sorted(positives)},
                    summary=(
                        f"first known-positive artifact at rank {rank}"
                        if rank
                        else "no known-positive artifact was returned"
                    ),
                    limitation="says nothing about results below the first positive",
                )
            )
    if not values:
        return MetricSet(
            aggregates=[
                _insufficient(
                    ctx,
                    "known_positive_reciprocal_rank",
                    replay.variant.variant_id,
                    formula="mean_q 1 / rank(first positive)",
                    reason="no query in this cohort has positive-proof artifacts",
                )
            ]
        )
    mean = _mean(values) or 0.0
    return MetricSet(
        aggregates=[
            MetricResult(
                metric_id="known_positive_reciprocal_rank",
                classification=Classification.DEMONSTRATED.value,
                scope=_scope(ctx, replay.variant.variant_id),
                value=mean,
                formula="mean_q 1 / rank(first positive)",
                substituted=f"mean over {len(values)} evidenced queries = {mean}",
                numerator=round(mean * len(values), 6),
                denominator=len(values),
                status=MetricStatus.INFORMATIONAL.value,
                summary=(
                    f"The first known-positive artifact appeared at a mean reciprocal rank of "
                    f"{mean:.3f} over {len(values)} evidenced queries."
                ),
                supports_claim="how early previously-useful content is reachable",
                does_not_support="anything about results ranked below the first positive",
            )
        ],
        per_query=per_query,
    )


def negative_exposure(ctx: MetricContext, replay: VariantReplay, k: int) -> MetricResult:
    """Known-negative results per served slot. Lower is better.

    Counted over slots rather than over queries because that is the reader's
    experience: two bad results in ten is the cost, whether they were for one
    query or two.
    """

    negatives_shown = 0
    slots = 0
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        negatives = set(ctx.negatives(query_id))
        if not negatives:
            continue
        top = result.top(k)
        slots += len(top)
        negatives_shown += sum(1 for node_id in top if node_id in negatives)
    if not slots:
        return _insufficient(
            ctx,
            f"negative_exposure_at_{k}",
            replay.variant.variant_id,
            formula="|R_k ∩ N_q| / k",
            reason="no query in this cohort has negative-proof artifacts",
        )
    value = round(negatives_shown / slots, 6)
    return MetricResult(
        metric_id=f"negative_exposure_at_{k}",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="sum_q |R_{q,k} ∩ N_q| / sum_q |R_{q,k}|",
        substituted=f"{negatives_shown} / {slots}",
        numerator=negatives_shown,
        denominator=slots,
        status=MetricStatus.INFORMATIONAL.value,
        summary=(
            f"{negatives_shown} of {slots} served slots held an artifact with negative proof."
        ),
        supports_claim="how often known-bad content is still being served",
        does_not_support="that the remaining slots were good; most are unjudged",
    )


def pairwise_proof_accuracy(ctx: MetricContext, replay: VariantReplay) -> MetricResult:
    """Share of positive/negative pairs the ranking orders correctly.

    Unreturned items take :data:`TERMINAL_RANK`, applied identically on both
    sides of every pair. That is what keeps the metric defined when a run
    returns neither member of a pair, without letting the convention decide the
    answer.
    """

    correct = 0
    total = 0
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        positives = ctx.positives(query_id)
        negatives = ctx.negatives(query_id)
        if not positives or not negatives:
            continue
        for positive in positives:
            p_rank = result.rank_of(positive) or TERMINAL_RANK
            for negative in negatives:
                n_rank = result.rank_of(negative) or TERMINAL_RANK
                total += 1
                if p_rank < n_rank:
                    correct += 1
    if not total:
        return _insufficient(
            ctx,
            "pairwise_proof_accuracy",
            replay.variant.variant_id,
            formula="correctly ordered (positive, negative) pairs / all such pairs",
            reason="no query has both positive and negative proof",
        )
    value = round(correct / total, 6)
    return MetricResult(
        metric_id="pairwise_proof_accuracy",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="sum_q sum_{p,n} 1[rank(p) < rank(n)] / sum_q |P_q||N_q|",
        substituted=f"{correct} / {total}",
        numerator=correct,
        denominator=total,
        status=MetricStatus.INFORMATIONAL.value,
        operands={"terminal_rank": TERMINAL_RANK},
        summary=f"{correct} of {total} positive/negative pairs were ordered correctly.",
        supports_claim="the ranking's ability to separate the two judged classes",
        does_not_support="where unjudged results belong relative to either class",
    )


def evidence_discounted_gain(ctx: MetricContext, replay: VariantReplay, k: int) -> MetricResult:
    """Rank-discounted sum of net proof utility, normalized against the judged pool.

    Deliberately **not** called nDCG. The ideal ordering is constructed only
    from items this query has judgments for, so the denominator is "the best
    arrangement of what we know about", not "the best possible result list".
    Calling it nDCG would import a promise about exhaustive relevance labels
    that no partial-judgment set can keep.
    """

    gains: list[float] = []
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        judged = ctx.judged(query_id)
        if not judged:
            continue
        actual = sum(
            ctx.utility(query_id, node_id) / math.log2(index + 1)
            for index, node_id in enumerate(result.top(k), start=1)
        )
        ideal_utilities = sorted(
            (ctx.utility(query_id, target) for target in judged), reverse=True
        )[:k]
        ideal = sum(
            utility / math.log2(index + 1)
            for index, utility in enumerate(ideal_utilities, start=1)
            if utility > 0
        )
        if ideal <= 0:
            continue
        gains.append(max(-1.0, min(1.0, actual / ideal)))
    if not gains:
        return _insufficient(
            ctx,
            f"evidence_discounted_gain_at_{k}",
            replay.variant.variant_id,
            formula="EDCG@k / IdealEDCG@k",
            reason="no query has a positive-utility judged pool to normalize against",
        )
    value = _mean(gains) or 0.0
    return MetricResult(
        metric_id=f"evidence_discounted_gain_at_{k}",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="mean_q ( sum_i u(d_i)/log2(i+1) ) / IdealEDCG@k",
        substituted=f"mean over {len(gains)} queries = {value}",
        numerator=round(value * len(gains), 6),
        denominator=len(gains),
        status=MetricStatus.INFORMATIONAL.value,
        summary=(
            f"Rank-weighted net proof utility reached {value:.1%} of the best arrangement of the "
            f"judged pool, over {len(gains)} queries."
        ),
        supports_claim="how well the ranking uses the evidence it has",
        does_not_support=(
            "a relevance nDCG -- the ideal is built from judged items only, not from the corpus"
        ),
    )


def binary_preference(ctx: MetricContext, replay: VariantReplay) -> MetricResult:
    """bpref: how many known negatives outrank each known positive.

    The one retrieval metric here that ignores unjudged results entirely rather
    than ranking around them, which makes it the right cross-check on a sparse
    cohort -- if it and the recall metrics disagree, the disagreement is about
    the unjudged mass.
    """

    scores: list[float] = []
    for query_id, result in replay.results.items():
        if result.failed:
            continue
        positives = ctx.positives(query_id)
        negatives = set(ctx.negatives(query_id))
        if not positives or not negatives:
            continue
        total = 0.0
        for positive in positives:
            p_rank = result.rank_of(positive) or TERMINAL_RANK
            above = sum(
                1 for negative in negatives if (result.rank_of(negative) or TERMINAL_RANK) < p_rank
            )
            total += 1.0 - min(above, len(positives)) / len(positives)
        scores.append(total / len(positives))
    if not scores:
        return _insufficient(
            ctx,
            "binary_preference",
            replay.variant.variant_id,
            formula="mean_q (1/|P_q|) sum_p (1 - min(|N_above(p)|,|P_q|)/|P_q|)",
            reason="no query has both positive and negative proof",
        )
    value = _mean(scores) or 0.0
    return MetricResult(
        metric_id="binary_preference",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=value,
        formula="mean_q (1/|P_q|) sum_p (1 - min(|N_above(p)|,|P_q|)/|P_q|)",
        substituted=f"mean over {len(scores)} queries = {value}",
        numerator=round(value * len(scores), 6),
        denominator=len(scores),
        status=MetricStatus.INFORMATIONAL.value,
        optional=True,
        summary=f"bpref {value:.3f} over {len(scores)} queries with both judgment classes.",
        supports_claim="ranking quality measured without inferring anything about unjudged items",
        does_not_support="anything about the unjudged majority of the corpus",
    )


# --------------------------------------------------------------------------
# 18. memory attribution and generalization


def paired_delta(
    ctx: MetricContext,
    metric_id: str,
    baseline: VariantReplay,
    treatment: VariantReplay,
    scorer: Any,
    *,
    label: str,
    limitation: str,
) -> MetricResult:
    """A treatment-minus-baseline difference over the queries both completed.

    The one shape every attribution metric in the report takes.
    ``scorer(query_id, replay)`` returns that query's score under one run or
    ``None`` when the query is not scorable, and a query is only counted when
    both sides score it -- otherwise the difference is between two different
    populations.

    Worst regressions travel with the mean, because an intervention that lifts
    twenty queries by 0.05 and destroys one is reported by its mean as a clear
    win, and by its worst case as the thing somebody has to look at.
    """

    both, reasons = paired_ids(baseline, treatment)
    deltas: list[tuple[str, float]] = []
    for query_id in both:
        before = scorer(query_id, baseline)
        after = scorer(query_id, treatment)
        if before is None or after is None:
            reasons["not_scorable"] = reasons.get("not_scorable", 0) + 1
            continue
        deltas.append((query_id, round(after - before, 6)))
    if not deltas:
        return _insufficient(
            ctx,
            metric_id,
            treatment.variant.variant_id,
            formula=f"M({treatment.variant.variant_id}) - M({baseline.variant.variant_id})",
            reason=f"no query was scorable under both {baseline.variant.variant_id} and "
            f"{treatment.variant.variant_id}",
        )
    values = [delta for _, delta in deltas]
    mean = _mean(values) or 0.0
    improved = sum(1 for value in values if value > 0)
    regressed = sum(1 for value in values if value < 0)
    worst = sorted(deltas, key=lambda item: item[1])[:5]
    return MetricResult(
        metric_id=metric_id,
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, treatment.variant.variant_id),
        value=mean,
        formula=(
            f"mean_q [ M_q({treatment.variant.variant_id}) - M_q({baseline.variant.variant_id}) ]"
        ),
        substituted=f"mean over {len(values)} paired queries = {mean}",
        numerator=round(mean * len(values), 6),
        denominator=len(values),
        status=MetricStatus.INFORMATIONAL.value,
        operands={
            "baseline_variant": baseline.variant.variant_id,
            "treatment_variant": treatment.variant.variant_id,
            "improved_queries": improved,
            "regressed_queries": regressed,
            "worst_regressions": [
                {"query_id": query_id, "delta": delta} for query_id, delta in worst if delta < 0
            ],
        },
        excluded_count=sum(reasons.values()),
        exclusion_reasons=reasons,
        summary=(
            f"{label}: mean change {mean:+.4f} over {len(values)} paired queries "
            f"({improved} improved, {regressed} regressed)."
        ),
        supports_claim=(
            f"the effect of the difference between {baseline.variant.variant_id} and "
            f"{treatment.variant.variant_id} on this cohort"
        ),
        does_not_support=limitation,
    )


def kprr_scorer(ctx: MetricContext) -> Any:
    """Per-query reciprocal rank of the first known positive, or ``None``."""

    def score(query_id: str, replay: VariantReplay) -> float | None:
        result = replay.results.get(query_id)
        if result is None or result.failed:
            return None
        positives = set(ctx.positives(query_id))
        if not positives:
            return None
        rank = next(
            (i for i, node_id in enumerate(result.ranked_ids, start=1) if node_id in positives),
            None,
        )
        return 1.0 / rank if rank else 0.0

    return score


def recall_scorer(ctx: MetricContext, k: int) -> Any:
    def score(query_id: str, replay: VariantReplay) -> float | None:
        result = replay.results.get(query_id)
        if result is None or result.failed:
            return None
        positives = ctx.positives(query_id)
        if not positives:
            return None
        return len(set(result.top(k)) & set(positives)) / len(positives)

    return score


def displacement(
    ctx: MetricContext, baseline: VariantReplay, treatment: VariantReplay, k: int
) -> list[MetricResult]:
    """What the treatment pushed out of the top k, and whether it mattered.

    Memory records compete for the same slots as corpus content, so a memory
    system that helps on average can still be evicting known-good documents.
    Total displacement is a cost measure; *positive* displacement is the one
    that is straightforwardly bad, and it is reported separately because the
    two are routinely confused.
    """

    both, reasons = paired_ids(baseline, treatment)
    displaced_total = 0
    slots = 0
    positive_displaced = 0
    positive_baseline = 0
    examples: list[dict[str, Any]] = []
    for query_id in both:
        before = baseline.results[query_id].top(k)
        after = set(treatment.results[query_id].top(k))
        dropped = [node_id for node_id in before if node_id not in after]
        displaced_total += len(dropped)
        slots += len(before)
        positives = set(ctx.positives(query_id))
        in_baseline = [node_id for node_id in before if node_id in positives]
        positive_baseline += len(in_baseline)
        lost = [node_id for node_id in in_baseline if node_id not in after]
        positive_displaced += len(lost)
        if lost and len(examples) < 10:
            examples.append({"query_id": query_id, "displaced_positive_ids": lost})

    total = MetricResult(
        metric_id=f"displacement_at_{k}",
        classification=Classification.STRUCTURAL.value,
        scope=_scope(ctx, treatment.variant.variant_id),
        value=round(displaced_total / slots, 6) if slots else None,
        formula="|C_k - M_k| / k",
        substituted=f"{displaced_total} / {slots}",
        numerator=displaced_total,
        denominator=slots,
        status=(
            MetricStatus.INFORMATIONAL.value if slots else MetricStatus.INSUFFICIENT_EVIDENCE.value
        ),
        excluded_count=sum(reasons.values()),
        exclusion_reasons=reasons,
        summary=f"{displaced_total} of {slots} baseline top-{k} slots changed occupant.",
        supports_claim="how much of the result list the treatment rewrote",
        does_not_support=(
            "whether the rewrite was an improvement; most displaced items are unjudged"
        ),
    )
    positive = MetricResult(
        metric_id=f"positive_displacement_at_{k}",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, treatment.variant.variant_id),
        value=round(positive_displaced / positive_baseline, 6) if positive_baseline else None,
        formula="|(C_k - M_k) ∩ P_q| / |C_k ∩ P_q|",
        substituted=f"{positive_displaced} / {positive_baseline}",
        numerator=positive_displaced,
        denominator=positive_baseline,
        status=(
            MetricStatus.INFORMATIONAL.value
            if positive_baseline
            else MetricStatus.INSUFFICIENT_EVIDENCE.value
        ),
        operands={"examples": examples},
        summary=(
            f"{positive_displaced} of {positive_baseline} known-positive artifacts that the "
            f"baseline ranked in its top {k} were pushed out by the treatment."
        ),
        supports_claim="the cost of the treatment in demonstrated-useful content",
        does_not_support="the cost in unjudged content, which is unmeasured",
    )
    return [total, positive]


def control_regression(
    ctx: MetricContext,
    baseline: VariantReplay,
    treatment: VariantReplay,
    *,
    tolerance: float = 0.0,
) -> MetricResult:
    """Share of *evidenced* control queries the treatment made worse.

    A gate input, not a score. Control queries are the ones the intervention
    should not touch, so a decline past tolerance is unintended by construction
    -- which is why this is a rate of *queries harmed* rather than a mean effect
    that a few large wins could mask.

    **A changed control result is not a regressed one.** An earlier version of
    this counted any movement in an unjudged control query's top-k as a
    regression, and it fired immediately on a region whose memory records
    legitimately matched a control query: the ranking changed, nothing said it
    changed for the worse, and the gate failed anyway. Counting "different" as
    "worse" is precisely the over-claim the rest of this module refuses, so
    unjudged control queries are now excluded from the rate and reported
    separately as ``unjudged_changed`` -- a real observation about blast radius,
    published as the unmeasured thing it is.
    """

    scorer = kprr_scorer(ctx)
    both, reasons = paired_ids(baseline, treatment)
    regressed = 0
    scored = 0
    unjudged_changed = 0
    unjudged_total = 0
    worst: list[tuple[str, float]] = []
    for query_id in both:
        before = scorer(query_id, baseline)
        after = scorer(query_id, treatment)
        if before is None or after is None:
            unjudged_total += 1
            if baseline.results[query_id].top(10) != treatment.results[query_id].top(10):
                unjudged_changed += 1
            reasons["no_proof_to_score"] = reasons.get("no_proof_to_score", 0) + 1
            continue
        scored += 1
        delta = after - before
        if delta < -tolerance:
            regressed += 1
            worst.append((query_id, round(delta, 6)))
    if not scored:
        result = _insufficient(
            ctx,
            "control_regression_rate",
            treatment.variant.variant_id,
            formula="|{q in Q_C : ΔM_q < -ε}| / |Q_C evidenced|",
            reason=(
                f"no control query carries proof: {unjudged_changed} of {unjudged_total} had "
                "their ranking changed by the treatment, but whether that is a regression is "
                "unmeasured"
            ),
        )
        result.operands = {
            "unjudged_changed": unjudged_changed,
            "unjudged_total": unjudged_total,
        }
        return result
    value = round(regressed / scored, 6)
    return MetricResult(
        metric_id="control_regression_rate",
        classification=Classification.DEMONSTRATED.value,
        scope=_scope(ctx, treatment.variant.variant_id),
        value=value,
        formula="|{q in Q_C : ΔM_q < -ε}| / |Q_C evidenced|",
        substituted=f"{regressed} / {scored}",
        numerator=regressed,
        denominator=scored,
        status=MetricStatus.PASS.value if regressed == 0 else MetricStatus.FAIL.value,
        threshold=tolerance,
        operands={
            "tolerance": tolerance,
            "worst": [
                {"query_id": qid, "delta": delta}
                for qid, delta in sorted(worst, key=lambda item: item[1])[:5]
            ],
            "unjudged_changed": unjudged_changed,
            "unjudged_total": unjudged_total,
        },
        excluded_count=sum(reasons.values()),
        exclusion_reasons=reasons,
        summary=(
            f"{regressed} of {scored} evidenced control queries changed for the worse under "
            f"{treatment.variant.variant_id}; {unjudged_changed} of {unjudged_total} unjudged "
            "control queries had their ranking changed at all."
        ),
        supports_claim="unintended harm on queries the intervention should not touch",
        does_not_support=(
            "whether the unjudged control queries whose ranking moved were helped or harmed"
        ),
    )


def generalization_gap(learned: MetricResult, holdout: MetricResult) -> MetricResult:
    """Learned-query gain minus forward-generalization gain.

    A large positive gap is the finding this cohort split exists to surface:
    the intervention helps the queries that created it and does not transfer.
    That is memorization, and it is not a reason to promote anything.
    """

    scope = holdout.scope
    if learned.value is None or holdout.value is None:
        return MetricResult(
            metric_id="generalization_gap",
            classification=Classification.DEMONSTRATED.value,
            scope=scope,
            value=None,
            formula="LearnedQueryGain - GeneralizationGain",
            substituted="not computed",
            denominator=0,
            status=MetricStatus.INSUFFICIENT_EVIDENCE.value,
            summary="one side of the comparison had no computable gain",
            supports_claim="nothing",
            does_not_support="a gap cannot be computed without both cohorts",
        )
    value = round(learned.value - holdout.value, 6)
    return MetricResult(
        metric_id="generalization_gap",
        classification=Classification.DEMONSTRATED.value,
        scope=scope,
        value=value,
        formula="LearnedQueryGain - GeneralizationGain",
        substituted=f"{learned.value:+.4f} - {holdout.value:+.4f}",
        numerator=value,
        denominator=1,
        status=MetricStatus.INFORMATIONAL.value,
        operands={
            "learned_gain": learned.value,
            "holdout_gain": holdout.value,
            "learned_denominator": learned.denominator,
            "holdout_denominator": holdout.denominator,
        },
        summary=(
            f"Gain on queries that created the memory exceeds gain on later, independent "
            f"queries by {value:+.4f}."
        ),
        supports_claim="how much of the measured benefit is recall of learned experience",
        does_not_support=(
            "a causal claim -- the two cohorts differ in their queries as well as in their "
            "relationship to the intervention"
        ),
    )


# --------------------------------------------------------------------------
# 19. operational


def latency(ctx: MetricContext, replay: VariantReplay) -> MetricResult:
    """Replay latency distribution: median, p95, max.

    Nearest-rank percentiles, stated so the convention is versioned rather than
    inferred from whichever implementation happened to run.
    """

    samples = replay.latencies()
    if not samples:
        return _insufficient(
            ctx,
            "replay_latency_ms",
            replay.variant.variant_id,
            formula="nearest-rank percentiles over per-query replay durations",
            reason="no query completed under this variant",
            classification=Classification.OPERATIONAL.value,
        )

    def percentile(fraction: float) -> float:
        index = max(0, math.ceil(fraction * len(samples)) - 1)
        return round(samples[index], 3)

    median = percentile(0.5)
    return MetricResult(
        metric_id="replay_latency_ms",
        classification=Classification.OPERATIONAL.value,
        scope=_scope(ctx, replay.variant.variant_id),
        value=median,
        formula="nearest-rank percentile over per-query replay durations",
        substituted=f"p50 over {len(samples)} queries = {median} ms",
        numerator=median,
        denominator=len(samples),
        unit="milliseconds",
        status=MetricStatus.INFORMATIONAL.value,
        operands={
            "p50_ms": median,
            "p95_ms": percentile(0.95),
            "max_ms": round(samples[-1], 3),
            "percentile_convention": "nearest-rank, version 1",
        },
        summary=(
            f"Replay latency p50 {median} ms, p95 {percentile(0.95)} ms, max "
            f"{round(samples[-1], 3)} ms over {len(samples)} queries."
        ),
        supports_claim="what this evaluation cost per query, on this host",
        does_not_support="production request latency; a replay has no HTTP or auth path",
    )


def growth(ctx: MetricContext, state: Any) -> MetricResult:
    """Counts an operator watches between runs.

    Informational by design. There is no threshold at which "the memory store
    grew" is wrong, and attaching one would turn a capacity signal into a false
    alarm.
    """

    def count(sql: str) -> int:
        try:
            rows = state.rows(sql)
            return int(rows[0]["c"]) if rows else 0
        except Exception:  # noqa: BLE001
            return 0

    counts = {
        "artifacts": count("SELECT COUNT(*) AS c FROM artifacts"),
        "chunks": count("SELECT COUNT(*) AS c FROM chunks"),
        "memory_records": count("SELECT COUNT(*) AS c FROM memory_records"),
        "memory_hot": count(
            "SELECT COUNT(*) AS c FROM memory_records WHERE tier IS NULL OR tier='hot'"
        ),
        "memory_cold": count("SELECT COUNT(*) AS c FROM memory_records WHERE tier='cold'"),
        "interaction_events": count("SELECT COUNT(*) AS c FROM interaction_events"),
        "evaluation_proofs": count("SELECT COUNT(*) AS c FROM evaluation_proofs"),
        "memory_candidates_pending": count(
            "SELECT COUNT(*) AS c FROM memory_candidates WHERE status='pending'"
        ),
    }
    return MetricResult(
        metric_id="growth",
        classification=Classification.OPERATIONAL.value,
        scope=_scope(ctx, None),
        value=float(counts["memory_records"]),
        formula="row counts at snapshot time",
        substituted=", ".join(f"{name}={value}" for name, value in counts.items()),
        numerator=counts["memory_records"],
        denominator=max(1, counts["artifacts"]),
        unit="count",
        status=MetricStatus.INFORMATIONAL.value,
        operands=counts,
        summary=(
            f"{counts['memory_records']} memory records ({counts['memory_hot']} hot, "
            f"{counts['memory_cold']} cold) against {counts['artifacts']} artifacts."
        ),
        supports_claim="how the stores are growing between snapshots",
        does_not_support="whether the growth is useful; that is what the attribution metrics ask",
    )
