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

from pheasant.search import fusion
from pheasant.search.ranking import RankingParameters

#: Arms in the order the merge walks them. The order matters: it decides which
#: arm's record wins a fused slot, and therefore which `kind` survives — so it
#: is re-exported from the merge rather than restated, for the same reason the
#: loop is.
ARM_ORDER: tuple[str, ...] = fusion.ARM_ORDER


def refuse(
    fusion_input: dict[str, list[list[str]]],
    max_results: int,
    ranking: RankingParameters,
    arms: tuple[str, ...] = ARM_ORDER,
) -> list[str]:
    """Reciprocal-rank fusion over captured arm lists. Returns fused ids.

    The tuning plane's entry point onto :func:`pheasant.search.fusion.fuse` —
    the same loop the query path runs, reading triples out of a captured
    explain block instead of projecting them from live records.

    It used to be a hand-maintained mirror of ``hybrid._merge_rrf``, described
    in its own docstring as "a line-for-line mirror ... every branch below
    corresponds to one there". That is the responsible version of a duplicate
    and it is still two loops: a change to one side passes review and diverges
    until :func:`verify_equivalence` happens to cover the case. There is one
    loop now, so the correspondence is not maintained, it is structural.

    ``arms`` restricts which arms contribute candidates at all, which is a
    different thing from weighting one to zero — see `fusion.fuse`, which
    carries the argument and the reasoning. That distinction is the tuning
    plane's own and is why this entry point exists rather than the caller
    using `fuse` directly.
    """

    candidates = {
        arm: [
            fusion.Candidate(key=key, node_id=node_id, kind=kind)
            for entry in (fusion_input.get(arm) or [])
            for key, node_id, kind in [(list(entry) + ["", "", ""])[:3]]
            if key
        ]
        for arm in arms
    }
    merged = fusion.fuse(candidates, max_results=max_results, ranking=ranking, arms=arms)
    return [item.node_id for item in merged.selected]


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

    What this checks changed when the two merges became one. It was a drift
    detector between two loops; there is one loop now, so a divergence can no
    longer be the merge disagreeing with itself. What it still checks is the
    thing that was always the larger risk: whether the **captured** candidate
    lists reproduce the query that ran. A truncated arm, a stage block from an
    older schema, or a capture taken around a filter that is not replayed all
    produce a faithful merge over unfaithful inputs — and a number that looks
    like a measurement. So it stays, and it stays load-bearing.
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
