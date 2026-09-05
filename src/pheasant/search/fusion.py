"""Reciprocal rank fusion, once.

The merge that turns three arms' orderings into one exists in two places for
one good reason: `search.hybrid` fuses *rich records* on the query path, and
`tuning.refusion` fuses *captured candidate lists* so a thousand parameter
points can be scored without running retrieval a thousand times. The second is
worth having — it is what makes the fusion family of the parameter search
affordable at all.

What was not worth having is two loops. The re-fusion was written as "a
line-for-line mirror" of the query-path merge, with a `verify_equivalence`
check to catch drift; that is the responsible version of a duplicate, and it
is still a place where a change to one side passes review and diverges until
the guard happens to cover the case.

So this module is the loop, and both are entry points onto it. The essence
either caller needs is the same three-field candidate — a fusion key, a
reporting identity, and a kind — because the merge keys on `chunk_id`, reports
on `node_id`, and breaks the dedup on `kind`. The query path projects its
records down to that, fuses, and maps the answer back to the records it
started with; the re-fusion reads the triples straight out of a captured
explain block.

What each caller keeps is what is genuinely its own: the query path keeps
"which of these dicts is the richest record for this key" and the mutation of
the returned rows; the re-fusion keeps arm *isolation*, which is a different
thing from a zero weight and is why it takes an `arms` argument at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pheasant.search.ranking import DEFAULT_RANKING, RankingParameters

#: The arms, in the order they contribute. Fixed rather than derived: the
#: representative rule below ("a text or vector row wins the slot from a graph
#: node") reads on arm identity, so the set is part of the contract.
ARM_ORDER: tuple[str, ...] = ("text", "vector", "graph")


@dataclass(frozen=True)
class Candidate:
    """One arm's nth result, reduced to what the merge actually reads.

    ``key`` is what the merge groups on (a chunk id, or a node id for graph
    hits). ``node_id`` is what a result reports as its identity and what the
    dedup below keys on. ``kind`` decides two branches: whether a graph node
    may be displaced by a richer record, and whether a row is exempt from
    node-level deduplication.
    """

    key: str
    node_id: str = ""
    kind: str = ""


@dataclass(frozen=True)
class Fused:
    """One item's position in the merged order, and how it got there."""

    key: str
    node_id: str
    kind: str
    score: float
    best_rank: int
    contributors: tuple[str, ...]
    #: Which arm supplied the record kept for this key, and its 0-based
    #: position in that arm's list. This is what lets the query path map an
    #: answer back to the original dict without the merge ever holding one.
    arm: str
    position: int

    @property
    def retrieved_by(self) -> str:
        return "+".join(self.contributors)


@dataclass(frozen=True)
class Fusion:
    """The whole merge: the full order, and what survives truncation.

    Both, because they answer different questions. ``ordered`` is what
    separates "fusion ranked it 47th" from "fusion ranked it 11th and
    max_results was 10" — two failures that look identical downstream and have
    nothing in common.
    """

    ordered: tuple[Fused, ...]
    selected: tuple[Fused, ...]


def fuse(
    candidates: Mapping[str, Sequence[Candidate]],
    *,
    max_results: int,
    ranking: RankingParameters = DEFAULT_RANKING,
    arms: tuple[str, ...] = ARM_ORDER,
) -> Fusion:
    """Fuse the arms on **rank**, not on their scores.

    The three retrievers score on scales that are not comparable, and merging
    them by raw score silently reduced hybrid to whichever arm scored highest
    in absolute terms. Measured on a real corpus: text (BM25-derived) returned
    0.86-0.92, vector (cosine) 0.6679-0.6735, graph a flat 0.60 — so text won
    every position, every time, and the other two arms cost latency while
    contributing nothing to the ordering. Worse, each arm's internal spread was
    tiny (vector separated unrelated files by 0.006), so even within an arm the
    numbers barely ranked anything.

    Reciprocal rank fusion sidesteps calibration entirely: an item scores
    ``sum(weight / (rrf_k + rank))`` over the arms that returned it, so only
    each arm's own ordering matters and agreement between arms is what promotes
    a result. That is the property hybrid search was supposed to have.

    ``arms`` restricts which arms contribute candidates at all, which is a
    different thing from weighting one to zero and the distinction is not
    academic. A zero weight is a zero *score*: the arm's candidates stay in the
    merge and are ordered by ``best_rank``, so zero-weighting two arms returns
    the third arm's candidates *plus theirs*, ordered by their original ranks.
    That is correct for tuning — an operator setting `vector_arm_weight: 0`
    wants the arm to stop influencing the order, not to have its documents
    vanish — and wrong for an ablation, which needs true isolation.

    Ordering is deterministic: ties break on the fused score, then on the best
    rank any arm gave the item, then on the fusion key.
    """

    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    contributors: dict[str, list[str]] = {}
    representative: dict[str, tuple[str, int, Candidate]] = {}

    for arm in arms:
        # An arm weight of 1.0 -- every arm's default -- makes this the
        # unweighted `1 / (k + rank)` the fusion always computed. The weights
        # exist because the diagnosis can tell "the arm never had it" from "the
        # arm had it and fusion buried it", and only the second is something a
        # fusion parameter can fix.
        arm_weight = ranking.arm_weight(arm)
        for position, candidate in enumerate(candidates.get(arm) or ()):
            key = candidate.key
            if not key:
                continue
            rank = position + 1
            scores[key] = scores.get(key, 0.0) + arm_weight / (ranking.rrf_k + rank)
            best_rank[key] = min(best_rank.get(key, rank), rank)
            if arm not in contributors.setdefault(key, []):
                contributors[key].append(arm)
            # Keep the richest record: chunk hits carry previews and line
            # ranges that graph hits do not, so a text/vector row wins the slot
            # even when the graph arm saw the item first.
            held = representative.get(key)
            if held is None or (arm != "graph" and held[2].kind == "node"):
                representative[key] = (arm, position, candidate)

    ordered = tuple(
        Fused(
            key=key,
            node_id=candidate.node_id,
            kind=candidate.kind,
            score=scores[key],
            best_rank=best_rank[key],
            contributors=tuple(sorted(contributors[key])),
            arm=arm,
            position=position,
        )
        for key, (arm, position, candidate) in sorted(
            representative.items(),
            key=lambda entry: (-scores[entry[0]], best_rank[entry[0]], entry[0]),
        )
    )

    selected: list[Fused] = []
    seen_nodes: set[str] = set()
    for item in ordered:
        if len(selected) >= max_results:
            break
        # A relationship row is exempt: several relationships legitimately
        # share one node id, and deduplicating them would collapse a set of
        # distinct edges into whichever one fused highest.
        if item.kind != "relationship" and item.node_id and item.node_id in seen_nodes:
            continue
        if item.node_id:
            seen_nodes.add(item.node_id)
        selected.append(item)

    return Fusion(ordered=ordered, selected=tuple(selected))
