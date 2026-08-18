"""Acceptance tests for Phase 35.4 — per-source leases and shard planning.

Two things this step exists to make true:

1. **Two indexers can write two different sources at once**, which is what the
   35.2 Postgres seam was for. One knowledge base, one writer process, was the
   ceiling.
2. **A split is a proposal you can read**, not a manual arithmetic exercise.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.backends import PostgresBackend
from pheasant.persistence.state_store import StateStore
from pheasant.sharding import SourceSize, plan_shards, render_plan
from pheasant.sync.locks import SourceLease, SourceLeaseError

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
postgres = pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run")


# ---------------------------------------------------------------------------
# Shard planning — offline, no database
# ---------------------------------------------------------------------------


def test_a_split_balances_by_packing_whole_sources() -> None:
    """Whole sources, never split, because cross-source edges live inside one.

    Hashing paths across shards balances perfectly and severs every
    `references`/`imports` edge — even balance is the wrong objective when the
    thing being balanced is a graph.
    """

    # Named so that alphabetical order is *ascending* size. A fixture whose
    # names happen to sort by descending size makes "largest-first" and "in
    # whatever order they arrived" indistinguishable — mutation testing caught
    # exactly that, and the first version of this test passed with the sort
    # replaced by name order.
    plan = plan_shards(
        [
            SourceSize("a", 3_000),
            SourceSize("b", 3_000),
            SourceSize("c", 3_000),
            SourceSize("d", 4_000),
            SourceSize("e", 5_000),
        ],
        shards=2,
    )
    assert plan["shard_count"] == 2
    placed = [name for shard in plan["shards"] for name in shard["sources"]]
    assert sorted(placed) == ["a", "b", "c", "d", "e"], "a source was dropped or duplicated"

    # The property LPT buys is balance. Ascending order gives 11k/7k here;
    # largest-first gives 10k/8k.
    sizes = sorted(shard["files"] for shard in plan["shards"])
    assert sizes[-1] - sizes[0] <= 2_000, f"poorly balanced split: {sizes}"
    assert sum(sizes) == 18_000

    # And whole sources, never split across regions.
    for shard in plan["shards"]:
        assert shard["files"] == sum(
            size
            for name, size in {"a": 3_000, "b": 3_000, "c": 3_000, "d": 4_000, "e": 5_000}.items()
            if name in shard["sources"]
        )


def test_the_shard_count_is_derived_from_the_node_budget() -> None:
    """Without an explicit count, pick the fewest regions that fit."""

    plan = plan_shards(
        [SourceSize(f"repo-{index}", 100_000) for index in range(6)],
        max_nodes_per_shard=1_500_000,
    )
    # 600k files -> 3.78M nodes; at 1.5M per shard that needs 3.
    assert plan["total_nodes"] == int(600_000 * 6.3)
    assert plan["shard_count"] == 3
    assert all(shard["nodes"] <= 1_500_000 for shard in plan["shards"])


def test_one_oversized_source_is_reported_not_silently_split() -> None:
    """No arrangement of whole sources fixes a single source over budget. The
    planner has to say so rather than propose a split it cannot honour."""

    plan = plan_shards([SourceSize("monorepo", 500_000)], max_nodes_per_shard=1_500_000)
    assert len(plan["shards"]) == 1
    assert plan["warnings"], "an over-budget source produced no warning"
    warning = plan["warnings"][0]
    assert "monorepo" in warning
    assert "include/exclude" in warning or "depth" in warning


def test_asking_for_more_shards_than_sources_says_so() -> None:
    plan = plan_shards([SourceSize("only", 1_000)], shards=4)
    assert len(plan["shards"]) == 1
    assert plan["shard_count"] == 1


def test_memory_recommendations_carry_headroom_and_round_to_real_sizes() -> None:
    """A 10% underestimate is an OOM kill mid-sync; a 50% overestimate is
    cheap. The recommendation is deliberately generous."""

    plan = plan_shards([SourceSize("repo", 240_000)], shards=1)
    shard = plan["shards"][0]
    assert shard["projected_rss_bytes"] > shard["nodes"] * 2400
    assert shard["recommended_memory"].endswith("Gi")
    # Comfortably above the raw projection, and a size a human would type.
    recommended_gib = float(shard["recommended_memory"].removesuffix("Gi"))
    assert recommended_gib >= shard["projected_rss_bytes"] / 1024**3


def test_the_rendered_plan_names_regions_sources_and_memory() -> None:
    text = render_plan(plan_shards([SourceSize("a", 9_000), SourceSize("b", 3_000)], shards=2))
    assert "shard-1" in text and "shard-2" in text
    assert "- a" in text and "- b" in text
    assert "Gi" in text
    assert "router" in text


def test_planning_an_empty_corpus_is_not_an_error() -> None:
    plan = plan_shards([SourceSize("empty", 0)])
    assert plan["shards"] == []
    assert plan["warnings"]
    assert "Nothing to plan" in render_plan(plan)


# ---------------------------------------------------------------------------
# Per-source leases — needs a real database
# ---------------------------------------------------------------------------


def _store() -> StateStore:
    store = StateStore(backend=PostgresBackend(DSN))
    store.migrate()
    return store


@postgres
def test_two_writers_can_hold_two_different_sources_at_once() -> None:
    """The ceiling this step lifts. One writer per knowledge base was the
    limit; two sources have no reason to wait for each other."""

    store = _store()
    try:
        docs = SourceLease(store, "docs", owner="writer-a")
        code = SourceLease(store, "code", owner="writer-b")
        assert docs.try_acquire() is True
        assert code.try_acquire() is True
        assert docs.held and code.held
    finally:
        docs.release()
        code.release()
        store.close()


@postgres
def test_two_writers_cannot_hold_the_same_source() -> None:
    """A source's artifacts, chunks, graph nodes and manifest are one
    consistent set; two writers would interleave them."""

    store = _store()
    try:
        first = SourceLease(store, "shared", owner="writer-a")
        second = SourceLease(store, "shared", owner="writer-b")
        assert first.try_acquire() is True
        assert second.try_acquire() is False
        assert second.held is False
        with pytest.raises(SourceLeaseError, match="shared"):
            second.acquire(wait_timeout_s=0)
    finally:
        first.release()
        store.close()


@postgres
def test_a_released_lease_is_immediately_available() -> None:
    store = _store()
    try:
        first = SourceLease(store, "handover", owner="writer-a")
        assert first.try_acquire() is True
        first.release()
        second = SourceLease(store, "handover", owner="writer-b")
        assert second.try_acquire() is True
        second.release()
    finally:
        store.close()


@postgres
def test_a_dead_writers_lease_is_taken_over_once_it_goes_stale() -> None:
    """Otherwise an OOM-killed indexer locks its source out forever."""

    store = _store()
    try:
        dead = SourceLease(store, "stale", owner="dead-writer", stale_after_s=0.0)
        assert dead.try_acquire() is True
        dead._stop.set()  # stop heartbeating, as a killed process would

        # stale_after_s=0 makes every heartbeat instantly ancient.
        taker = SourceLease(store, "stale", owner="live-writer", stale_after_s=0.0)
        assert taker.try_acquire() is True
        rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", ("stale",))
        assert rows[0]["owner"] == "live-writer"
        taker.release()
    finally:
        store.close()


@postgres
def test_releasing_a_lease_we_already_lost_does_not_steal_it_back() -> None:
    """A takeover means someone else owns the row. Deleting it on our way out
    would hand a third writer a source that is actively in use."""

    store = _store()
    try:
        loser = SourceLease(store, "contested", owner="loser", stale_after_s=0.0)
        assert loser.try_acquire() is True
        loser._stop.set()
        winner = SourceLease(store, "contested", owner="winner", stale_after_s=0.0)
        assert winner.try_acquire() is True

        loser.release()  # the loser tidying up after itself

        rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", ("contested",))
        assert rows and rows[0]["owner"] == "winner", "the loser deleted the winner's lease"
        winner.release()
    finally:
        store.close()


@postgres
def test_concurrent_claims_produce_exactly_one_winner() -> None:
    """The exclusion is a single conditional UPDATE precisely so the database
    arbitrates; a read-then-write in Python would let both threads win."""

    store = _store()
    winners: list[str] = []
    held: list[SourceLease] = []
    lock = threading.Lock()
    # Every thread claims at the same instant. Without this they queue up, and
    # since a winner that releases frees the row, several would legitimately
    # "win" in turn — which is not the property under test. (That is exactly
    # what the first version of this test measured, and it looked like a
    # concurrency bug in the lease rather than a bug in the test.)
    start = threading.Barrier(6)

    def claim(name: str) -> None:
        lease = SourceLease(StateStore(backend=PostgresBackend(DSN)), "race", owner=name)
        start.wait(timeout=10)
        won = lease.try_acquire()
        with lock:
            if won:
                winners.append(name)
                # Hold it. Releasing here would let the next thread win too.
                held.append(lease)
        if not won:
            lease.state.close()

    try:
        threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"
        rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", ("race",))
        assert rows and rows[0]["owner"] == winners[0]
    finally:
        for lease in held:
            lease.release()
            lease.state.close()
        store.close()


def test_sqlite_keeps_the_whole_state_lease(tmp_path: Path) -> None:
    """Rule 7. SQLite genuinely permits one writer per file, so the
    whole-state lease there is an accurate model, not a limitation to route
    around. Runs everywhere, because that is the default deployment."""

    from pheasant.sync.engine import SyncEngine

    workspace = tmp_path / "docs"
    workspace.mkdir()
    (workspace / "a.md").write_text("# A\n\nGateway notes.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    try:
        assert engine.state.dialect.name == "sqlite"
        assert engine.sync_source("docs", "full").indexed_artifacts == 1
        # The whole-state lease was taken, and no per-source rows were written.
        assert engine.lease.held
        assert engine.state.rows("SELECT source_id FROM source_leases") == []
    finally:
        engine.close()
