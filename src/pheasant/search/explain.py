"""The stage block: what a search did, in the shape the diagnosis reads.

``HybridSearch.search_context(explain=True)`` returns this beside the results,
and `tuning.stages` attributes every miss from it. That makes it one of the
most consequential interfaces in the system — the entire retrieval diagnosis
rests on it — and until now its shape was declared nowhere. A renamed key
produced a *wrong diagnosis*, not an error: the consumer's ``.get()`` returned
its default, the stage looked empty, and the plane confidently blamed
something else.

Two things are declared here, and the second is the one with teeth.

The :class:`StageBlock` TypedDict is the shape. It costs nothing at runtime
(a TypedDict *is* a dict) and it gives a reader one place to see what the
block contains, rather than reconstructing it from a producer 200 lines long
and a consumer in another package.

:data:`REQUIRED_KEYS` and :data:`CONSUMED_KEYS` are the enforcement, because
this repository runs no type checker: `tests/test_search_context.py` asserts
that what the producer writes and what the diagnosis reads are the same set of
names. A key renamed on one side fails there instead of quietly degrading a
measurement.
"""

from __future__ import annotations

from typing import Any, TypedDict


class QueryStage(TypedDict, total=False):
    """What was asked, and how far past it the arms were told to fetch."""

    text: str
    mode: str
    max_results: int
    fetch_n: int
    over_fetching: bool
    steering: dict[str, Any]


class FusionStage(TypedDict, total=False):
    """The merged order *before* truncation, and the parameters that made it.

    ``ranked`` is what separates "fusion ranked it 47th" from "fusion ranked it
    11th and max_results was 10" — two failures that look identical downstream
    and have nothing in common.
    """

    ranked: list[str]
    scores: dict[str, float]
    contributors: dict[str, list[str]]
    rrf_k: float
    arm_weights: dict[str, float]


class StageBlock(TypedDict, total=False):
    """One search, stage by stage.

    ``total=False`` throughout and deliberately: several keys appear only when
    the search took that path at all (``fusion_input`` only when the capture is
    re-fusable, ``arms_failed`` only when an arm ran). The consumer treats a
    missing key as "that stage did not happen", which is why every read there
    is a ``.get`` with a default — and why the key *names* have to be pinned by
    something, since a typo reads as an absence.
    """

    #: The ranking point this search fused under.
    parameters: dict[str, Any]
    query: QueryStage
    #: Per arm, the ids each retriever could find at all. A target absent here
    #: was never a candidate — an indexing or chunking problem, not a ranking
    #: one.
    candidates: dict[str, list[str]]
    #: Per arm, what is left after the filters applied so far.
    surviving: dict[str, list[str]]
    #: Per filter name, per arm, the ids that filter removed.
    filters: dict[str, dict[str, list[str]]]
    #: Artifact path per id, so a diagnosis can name a file rather than a hash.
    paths: dict[str, str]
    arms_run: list[str]
    #: Arms that raised. Distinct from an arm that returned nothing: "the
    #: vector index is down" and "it has nothing for this query" call for
    #: opposite responses.
    arms_failed: list[str]
    fusion: FusionStage
    #: The captured per-arm candidate triples a re-fusion replays.
    fusion_input: dict[str, list[list[str]]]
    fusion_input_truncated: bool
    max_results: int
    #: The ids actually served, in order.
    returned: list[str]


#: Keys the producer writes on every explained search. Absent ones are
#: path-dependent and are not listed: asserting on those would fail for a
#: text-only region rather than for a defect.
REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "parameters",
        "query",
        "candidates",
        "surviving",
        "filters",
        "paths",
        "arms_run",
        "arms_failed",
        "fusion",
        "returned",
    }
)

#: Keys the diagnosis reads. Every one must be something the producer can
#: write, or the stage it names is permanently invisible.
CONSUMED_KEYS: frozenset[str] = frozenset(
    {
        "candidates",
        "surviving",
        "filters",
        "fusion",
        "returned",
        "paths",
        "arms_run",
        "arms_failed",
        "max_results",
        "fusion_input",
        "fusion_input_truncated",
        "query",
        "parameters",
    }
)
