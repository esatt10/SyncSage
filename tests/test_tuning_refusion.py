"""The cheap path must be the same path.

Offline re-fusion is what makes a parameter search affordable: the fusion
family is evaluated by re-running the merge over cached arm lists instead of
re-running retrieval. That is only sound if the re-implementation *is* the
implementation, and this repository has already paid for the alternative --
a hand-rolled ``yaml.py`` shadowed the real parser and concealed four bugs
before it was deleted.

So the load-bearing assertion here is not "re-fusion produces plausible
results". It is: **re-fuse the captured inputs at the parameters that actually
ran, and get back exactly what the region served** -- id for id, order
included, against a real indexed corpus through the real
``HybridSearch.search_context``.
"""

from __future__ import annotations

from typing import Any

import pytest

from pheasant.search.hybrid import HybridSearch
from pheasant.search.ranking import RankingParameters
from pheasant.search.sqlite_store import SearchStore
from pheasant.tuning import refusion
from tests.conftest import run_sync

QUERIES = (
    "sync engine",
    "how does indexing work",
    "readme",
    "search context provenance",
    "configuration",
)


def _searcher(engine: Any, ranking: RankingParameters | None = None) -> HybridSearch:
    return HybridSearch(
        SearchStore(engine.state, ranking=ranking),
        vector=engine.vector_searcher(),
        node_index=getattr(engine, "node_index", None),
    )


@pytest.fixture()
def indexed(loaded_config: Any, sync_engine: Any) -> Any:
    run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    return sync_engine


def test_explain_is_absent_unless_asked_for(indexed: Any, loaded_config: Any) -> None:
    """The ordinary payload must not change because a diagnostic exists.

    Same rule ``heading_path`` and ``memory_policy`` follow: a key appears
    only when it says something. A production caller that never asks for
    stages must get the response it got before this plane was written.
    """

    searcher = _searcher(indexed)
    plain = searcher.search_context(loaded_config.knowledge_base_id, "sync engine")
    assert "stages" not in plain

    explained = searcher.search_context(
        loaded_config.knowledge_base_id, "sync engine", explain=True
    )
    assert "stages" in explained
    # ...and asking for it must not change the answer.
    assert [item["node_id"] for item in explained["results"]] == [
        item["node_id"] for item in plain["results"]
    ]


@pytest.mark.parametrize("query", QUERIES)
def test_refusion_reproduces_the_served_order_exactly(
    indexed: Any, loaded_config: Any, query: str
) -> None:
    """The equivalence the whole cheap path rests on."""

    searcher = _searcher(indexed)
    payload = searcher.search_context(
        loaded_config.knowledge_base_id, query, max_results=10, explain=True
    )
    stages = payload["stages"]
    if not payload["results"]:
        pytest.skip(f"corpus returned nothing for {query!r}")

    ok, reason = refusion.verify_equivalence(stages, searcher.ranking_parameters())
    assert ok, reason

    served = [item["node_id"] for item in payload["results"]]
    computed = refusion.refuse(
        stages["fusion_input"], stages["max_results"], searcher.ranking_parameters()
    )
    assert computed == served


def test_refusion_matches_a_real_search_under_changed_parameters(
    indexed: Any, loaded_config: Any
) -> None:
    """The point of the whole thing: a *predicted* ranking must be the real one.

    Re-fusion is only useful if a parameter point it scores well is a point the
    region would really serve that way. So this runs the expensive path -- a
    genuine search with the trial's parameters pinned into the store -- and
    requires the cheap path to have predicted it.
    """

    baseline = _searcher(indexed)
    payload = baseline.search_context(
        loaded_config.knowledge_base_id, "sync engine", max_results=10, explain=True
    )
    if len(payload["results"]) < 2:
        pytest.skip("need at least two results to tell orderings apart")

    for point in (
        RankingParameters(rrf_k=5.0),
        RankingParameters(rrf_k=200.0),
        RankingParameters(text_arm_weight=3.0, graph_arm_weight=0.0),
        RankingParameters(vector_arm_weight=0.0),
    ):
        predicted = refusion.refuse(
            payload["stages"]["fusion_input"], payload["stages"]["max_results"], point
        )
        actual = _searcher(indexed, ranking=point).search_context(
            loaded_config.knowledge_base_id, "sync engine", max_results=10
        )
        assert predicted == [item["node_id"] for item in actual["results"]], (
            f"re-fusion mispredicted the ranking under {point.values()}"
        )


def test_refusion_refuses_a_capture_it_cannot_reproduce() -> None:
    """A degraded capture must fall back, never approximate.

    Each of these returns a number if the guard is missing, and the number
    looks exactly like a measurement.
    """

    assert not refusion.refusable({})
    assert not refusion.refusable({"fusion_input": {}, "max_results": 10})
    assert not refusion.refusable({"fusion_input": {"text": [["a", "a", "chunk"]]}})
    assert not refusion.refusable(
        {
            "fusion_input": {"text": [["a", "a", "chunk"]]},
            "max_results": 10,
            "fusion_input_truncated": True,
        }
    )
    ok, reason = refusion.verify_equivalence({}, RankingParameters())
    assert not ok and reason


def test_arm_weight_of_zero_removes_an_arm_from_the_merge() -> None:
    """Zero is a real value on the ladder, and it has to mean 'ignore this arm'.

    Proving a merge is better *without* a stale vector index is a legitimate
    outcome of a tuning pass, so the parameter has to be able to express it.
    """

    fusion_input = {
        "text": [["c1", "n1", "chunk"]],
        "vector": [["c2", "n2", "chunk"]],
        "graph": [],
    }
    both = refusion.refuse(fusion_input, 10, RankingParameters())
    assert set(both) == {"n1", "n2"}

    text_only = refusion.refuse(fusion_input, 10, RankingParameters(vector_arm_weight=0.0))
    # The arm still contributes its candidate — a zero weight is a zero score,
    # not a filter — but it can no longer outrank a weighted arm.
    assert text_only[0] == "n1"
