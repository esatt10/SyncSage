"""Which step of retrieval lost the document.

This module is the reason the plane exists. Everything downstream -- which
parameters to propose, whether a trial is even relevant, what a report says --
follows from getting this attribution right, and getting it right is mostly a
matter of refusing to guess.

The pipeline, in the order a query passes through it:

=========================  ====================================================
Stage                      It failed here when...
=========================  ====================================================
``absent_from_corpus``     the target is in no arm's candidates and no arm
                           could have had it -- it is not indexed
``lexical_candidates``     the text arm did not return it, though other arms
                           did or the corpus contains it
``vector_candidates``      the vector arm did not return it
``graph_candidates``       the graph arm did not return it
``filters``                an arm *had* it and a filter removed it (ACL,
                           memory policy, section)
``fusion``                 it survived every filter and the fused order put it
                           below results that arms ranked worse
``truncation``             it was fused above the cut and ``max_results`` cut
                           it off anyway
``served``                 it was returned; nothing failed
=========================  ====================================================

**Attribution is to the first stage that lost it, and the order above is that
order.** A document the lexical arm never saw is not also a fusion failure,
even though it is trivially true that fusion did not rank it. Counting it as
both would produce a histogram where the totals exceed the misses and every
stage looks guilty, which is the same as no diagnosis at all.

**The arms are attributed independently, and then reconciled.** In ``hybrid``
mode a target missing from the vector arm but present in the text arm is not a
retrieval failure at all -- the pipeline worked. So a per-arm miss is only
*reported* (it is real information: it is how you learn the vector index is
stale) and only becomes the attributed cause when **no** arm had the target.
Blaming one arm for a miss another arm covered is how a diagnosis ends up
recommending a re-embed that changes nothing.

**Three things it refuses to conclude.**

*It never infers absence from the corpus.* "No arm returned it" and "it is not
indexed" are different claims, and the second needs a lookup, not an inference.
:func:`attribute` takes an ``indexed`` predicate; without one it reports
``candidates_missing`` -- an honest "no arm had it, and I did not check why" --
rather than upgrading a guess to a finding.

*It never attributes a query with no known positive.* A query nobody has said
anything about has no target, so there is no miss to explain. Those queries are
counted in a separate ``unevidenced`` bucket and excluded from the denominator,
because a histogram that treated them as successes would improve every time the
region served a query nobody evaluated.

*It never reads a score threshold as a failure.* Fused RRF scores have no
absolute scale -- the same rule the formation plane's ``retrieval-gap-v1``
follows. A stage failed because the document is in the wrong place, or absent,
never because a number was small.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pheasant.tuning.contracts import StageAttribution

#: Pipeline order. Attribution walks this list and stops at the first stage
#: that can be shown to have lost the document, so the order *is* the
#: precedence rule and changing it changes every diagnosis.
STAGES: tuple[str, ...] = (
    "absent_from_corpus",
    "candidates_missing",
    "lexical_candidates",
    "vector_candidates",
    "graph_candidates",
    "filters",
    "fusion",
    "truncation",
    "served",
)

#: Stages a parameter change in this package can plausibly move. The rest are
#: reported and then explicitly disclaimed: no ranking parameter fixes a
#: document that was never indexed, and a report that let an operator think
#: otherwise would be worse than no report.
ACTIONABLE_STAGES: frozenset[str] = frozenset(
    {"lexical_candidates", "vector_candidates", "filters", "fusion", "truncation"}
)

#: The arm each candidate stage belongs to.
ARM_STAGES: dict[str, str] = {
    "text": "lexical_candidates",
    "vector": "vector_candidates",
    "graph": "graph_candidates",
}


def _position(ordered: Iterable[str], target: str) -> int | None:
    for index, item in enumerate(ordered, start=1):
        if item == target:
            return index
    return None


def attribute(
    stages: dict[str, Any],
    target_id: str,
    *,
    max_results: int,
    query_id: str = "",
    indexed: Callable[[str], bool] | None = None,
) -> StageAttribution:
    """Where one query lost one known-positive target.

    ``stages`` is the block ``HybridSearch.search_context(explain=True)``
    returns. ``target_id`` is an artifact id -- the same identity the
    evaluation plane's proofs and replays use, which is what lets a diagnosis
    be joined to the evidence that made the query worth diagnosing.
    """

    candidates: dict[str, list[str]] = stages.get("candidates") or {}
    surviving: dict[str, list[str]] = stages.get("surviving") or {}
    filters: dict[str, dict[str, list[str]]] = stages.get("filters") or {}
    fusion: dict[str, Any] = stages.get("fusion") or {}
    returned: list[str] = list(stages.get("returned") or [])
    paths: dict[str, str] = stages.get("paths") or {}

    arms_with_target = sorted(arm for arm, ids in candidates.items() if target_id in ids)
    arms_run = sorted(stages.get("arms_run") or candidates.keys())
    arms_missing = sorted(set(arms_run) - set(arms_with_target))
    detail: dict[str, Any] = {
        "arms_run": arms_run,
        "arms_with_target": arms_with_target,
        "arms_missing_target": arms_missing,
        "arms_failed": sorted(stages.get("arms_failed") or []),
        "path": paths.get(target_id, ""),
    }

    def result(stage: str, reason: str, **extra: Any) -> StageAttribution:
        return StageAttribution(
            query_id=query_id,
            target_id=target_id,
            stage=stage,
            reason=reason,
            actionable=stage in ACTIONABLE_STAGES,
            detail={**detail, **extra},
        )

    # --- served, and at what rank ---------------------------------------
    served_rank = _position(returned, target_id)
    if served_rank is not None:
        return result(
            "served",
            f"returned at rank {served_rank}",
            served_rank=served_rank,
        )

    # --- no arm had it at all -------------------------------------------
    if not arms_with_target:
        if indexed is not None and not indexed(target_id):
            return result(
                "absent_from_corpus",
                "the target is not in the index, so no arm could return it",
            )
        # Deliberately not "absent_from_corpus": no arm returning a document
        # is consistent with the document being absent AND with every arm
        # ranking it past `fetch_n`. Without a corpus lookup those are
        # indistinguishable, and this plane does not guess.
        if indexed is None:
            return result(
                "candidates_missing",
                "no arm returned the target and the corpus was not checked",
            )
        # It is indexed and still no arm found it: the failure is real, and it
        # belongs to whichever arms were actually running. Attribute to the
        # lexical arm when it ran, because that is the arm every mode has and
        # the only one a region without embeddings even has to tune.
        for arm in ("text", "vector", "graph"):
            if arm in arms_run:
                return result(
                    ARM_STAGES[arm],
                    f"indexed, but no arm returned it within the {arm} arm's fetch window",
                )
        return result("candidates_missing", "no arm ran for this query")

    # --- an arm had it: did a filter take it away? -----------------------
    still_present = any(target_id in ids for ids in surviving.values())
    if not still_present:
        removed_by = sorted(
            name
            for name, per_arm in filters.items()
            if any(target_id in dropped for dropped in per_arm.values())
        )
        return result(
            "filters",
            "an arm returned the target and "
            + (f"the {', '.join(removed_by)} filter removed it" if removed_by else "it was dropped")
            + " before fusion",
            removed_by=removed_by,
        )

    # --- it reached fusion ------------------------------------------------
    fused = list(fusion.get("ranked") or [])
    fused_rank = _position(fused, target_id)
    if fused_rank is None:
        # It survived the filters and is absent from the fused list. The only
        # way that happens is the fused list being capped for reporting, so
        # say that rather than inventing a stage.
        return result(
            "fusion",
            "survived the filters but does not appear in the reported fused order",
        )
    if fused_rank > max_results:
        return result(
            "fusion",
            f"fused at rank {fused_rank}, below the {max_results} results requested",
            fused_rank=fused_rank,
            scores=fusion.get("scores", {}).get(target_id),
        )
    # Fused inside the cut and still not returned: the merge's own
    # node-deduplication dropped it behind another chunk of the same artifact.
    return result(
        "truncation",
        f"fused at rank {fused_rank} but not returned; the merge deduplicated it away",
        fused_rank=fused_rank,
    )


def stage_histogram(attributions: Iterable[StageAttribution]) -> dict[str, Any]:
    """The diagnosis: how the misses are distributed over the stages.

    Carries its own denominator and separates the actionable share from the
    rest, because "43% of misses are in fusion" and "43% of misses are
    documents that were never indexed" are the same sentence shape and
    opposite instructions.
    """

    counts: dict[str, int] = {stage: 0 for stage in STAGES}
    total = 0
    for item in attributions:
        counts[item.stage] = counts.get(item.stage, 0) + 1
        total += 1
    misses = total - counts.get("served", 0)
    actionable = sum(counts.get(stage, 0) for stage in ACTIONABLE_STAGES)
    ranked = sorted(
        ((stage, count) for stage, count in counts.items() if count and stage != "served"),
        key=lambda pair: (-pair[1], STAGES.index(pair[0]) if pair[0] in STAGES else 99),
    )
    return {
        "counts": {stage: count for stage, count in counts.items() if count},
        "evaluated": total,
        "served": counts.get("served", 0),
        "misses": misses,
        "actionable_misses": actionable,
        # The share of *misses* a parameter change could plausibly move. When
        # this is low the honest recommendation is to stop tuning and go look
        # at indexing, and the runner says exactly that rather than searching
        # a space that cannot contain the answer.
        "actionable_share": (actionable / misses) if misses else None,
        "dominant_stage": ranked[0][0] if ranked else None,
        "ranked": [{"stage": stage, "count": count} for stage, count in ranked],
    }
