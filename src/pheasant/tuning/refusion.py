"""Re-run the fusion under different parameters, without running retrieval.

This is the lightweight dynamic method the batch design would otherwise need,
and it comes from a structural fact rather than an approximation: the fusion
parameters act *after* the arms have produced their candidates. ``rrf_k`` and
the three arm weights cannot change what the lexical, vector or graph arm
returned, because none of them appears anywhere in the SQL, the embedding
lookup or the graph walk. They appear in one loop, over lists that are already
in hand.

So a trial over the fusion family costs no retrieval at all. One baseline
replay captures every arm's ordered candidates; every point in the fusion
subspace is then evaluated by re-running that loop. A thousand trials over four
parameters cost exactly one replay -- which is the difference between a tuning
pass that runs in a minute on a laptop and one that needs a fleet and an
afternoon.

**It is a re-implementation, and that is the risk.** This repository has
already lost time to a hand-rolled ``yaml.py`` that shadowed the real parser and
made a whole suite validate against something the image never ran. The same
mistake here would be worse, because it would be invisible: the re-fusion would
report improvements for parameter points that do nothing in production.

Three things hold it down.

*The captured inputs are the merge's own inputs.* ``fusion_input`` is written
from inside ``_merge_rrf`` itself, over the very lists it is about to fuse --
post-filter, post-ACL, post-prefer, in arm order. Nothing is reconstructed.

*Equivalence is asserted, not assumed.* :func:`verify_equivalence` re-fuses at
the baseline parameters and compares the result to what the region actually
returned, id for id. The runner calls it on every baseline replay before it
trusts a single cheap trial, and ``tests/test_tuning_refusion.py`` pins it
against the real search path on a real corpus.

*It refuses rather than approximates.* A truncated capture, a missing block, an
arm the reporting cap cut short -- each returns ``None``, and the caller falls
back to a real re-query. A cheap path that silently degrades into a wrong
answer is worse than an expensive one.
"""

from __future__ import annotations

from typing import Any

from pheasant.search.ranking import RankingParameters

#: Arms in the order ``_merge_rrf`` walks them. The order matters: it decides
#: which arm's record wins a fused slot, and therefore which `kind` survives.
ARM_ORDER: tuple[str, ...] = ("text", "vector", "graph")


def refuse(
    fusion_input: dict[str, list[list[str]]],
    max_results: int,
    ranking: RankingParameters,
) -> list[str]:
    """Reciprocal-rank fusion over captured arm lists. Returns fused ids.

    A line-for-line mirror of :func:`pheasant.search.hybrid._merge_rrf` over
    the triples ``(fusion key, reporting identity, kind)``. Every branch below
    corresponds to one there, and the correspondence is what
    :func:`verify_equivalence` checks -- so if that function is passing, this
    is the merge.
    """

    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    identity: dict[str, str] = {}
    kinds: dict[str, str] = {}

    for arm in ARM_ORDER:
        arm_weight = ranking.arm_weight(arm)
        for rank, entry in enumerate(fusion_input.get(arm) or [], start=1):
            key, node_id, kind = (list(entry) + ["", "", ""])[:3]
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + arm_weight / (ranking.rrf_k + rank)
            best_rank[key] = min(best_rank.get(key, rank), rank)
            # "Keep the richest record": a text or vector row wins the slot
            # from a graph node even when the graph arm saw the item first.
            if key not in identity or (arm != "graph" and kinds.get(key) == "node"):
                identity[key] = node_id
                kinds[key] = kind

    ordered = sorted(identity, key=lambda key: (-scores[key], best_rank[key], key))

    results: list[str] = []
    seen_nodes: set[str] = set()
    for key in ordered:
        if len(results) >= max_results:
            break
        node_id = identity[key]
        if kinds.get(key) != "relationship" and node_id and node_id in seen_nodes:
            continue
        if node_id:
            seen_nodes.add(node_id)
        results.append(node_id)
    return results


def refusable(stages: dict[str, Any]) -> bool:
    """Whether this captured query may be re-fused at all.

    Deliberately strict. Every ``False`` here sends the caller down the real
    re-query path, which is slower and always correct; a ``True`` that should
    have been ``False`` produces a number that looks like a measurement.
    """

    if not stages:
        return False
    if stages.get("fusion_input_truncated"):
        return False
    fusion_input = stages.get("fusion_input")
    if not isinstance(fusion_input, dict) or not any(fusion_input.values()):
        return False
    return bool(stages.get("max_results"))


def verify_equivalence(stages: dict[str, Any], ranking: RankingParameters) -> tuple[bool, str]:
    """Re-fuse at the parameters that actually ran and compare, id for id.

    Returns ``(True, "")`` or ``(False, reason)``. The reason names the first
    divergence rather than reporting a count, because a re-fusion that differs
    at rank 1 and a re-fusion that differs at rank 9 are different bugs.
    """

    if not refusable(stages):
        return False, "capture is not re-fusable"
    served = [str(item) for item in stages.get("returned") or []]
    computed = refuse(
        stages["fusion_input"],
        int(stages.get("max_results") or len(served) or 10),
        ranking,
    )
    if computed == served:
        return True, ""
    for index, (left, right) in enumerate(zip(computed, served, strict=False), start=1):
        if left != right:
            return False, (
                f"re-fusion diverges at rank {index}: computed {left!r}, region served {right!r}"
            )
    return False, (f"re-fusion returned {len(computed)} ids, the region served {len(served)}")


def restage(
    stages: dict[str, Any], fused_ids: list[str], ranking: RankingParameters
) -> dict[str, Any]:
    """A stage block describing a re-fused result, for attribution.

    Only ``returned`` and the fused order change: the arms ran once and their
    candidates are the same under every fusion parameter, which is the whole
    premise. Rebuilding the block this way lets a re-fused trial go through the
    identical :func:`pheasant.tuning.stages.attribute` path as a real one, so a
    cheap trial and an expensive one produce histograms that can be compared.
    """

    rebuilt = dict(stages)
    rebuilt["returned"] = list(fused_ids)
    fusion = dict(stages.get("fusion") or {})
    # The pre-truncation order under the new parameters, recomputed at the
    # full captured depth so a `fusion` vs `truncation` attribution stays
    # meaningful rather than collapsing to whatever survived the cut.
    fusion_input = stages.get("fusion_input") or {}
    deep = refuse(
        fusion_input,
        max(len(fused_ids), sum(len(entries) for entries in fusion_input.values())) or 1,
        ranking,
    )
    fusion["ranked"] = deep
    rebuilt["fusion"] = fusion
    return rebuilt
