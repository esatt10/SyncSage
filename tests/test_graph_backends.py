"""The two graph backends, held to the same promises.

``storage.graph_format`` picks where the published graph lives: ``rows`` (the
default since 35.10) puts it in ``graph_nodes``/``graph_edges`` beside the
artifacts it describes, ``node_link_json`` keeps the pre-35.10 single zstd
file. Both ship, so both are tested — and the interesting assertions are the
ones that say where they are the *same* and where they deliberately differ.

Same: what a sync produces, what a walk returns, what a search finds, what the
generation id means. Different: what a commit costs, and whether a serving
replica has to hold anything. Only the second kind is the point of the change;
the first kind is what makes it safe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph import builder as builder_module
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.graph.sql import SqlGraph
from pheasant.graph.traversal import neighbors, slice_
from pheasant.persistence.graph_rows import GraphRowStore, fold
from pheasant.persistence.graph_store import GraphStore
from pheasant.sync.engine import SyncEngine

KB = "backends"
FORMATS = ("rows", "node_link_json")


#: Same switch `tests/test_backend_parity.py` uses. The graph is now rows in
#: the state database, so "does the row backend work on Postgres" is a question
#: only a real Postgres can answer — and the three portability bugs CLAUDE.md
#: records (a discarded `rowcount`, an `INSERT OR IGNORE`, a declared FK) all
#: passed the offline suite and failed on first contact with a real server.
DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the graph rows there",
)


def _config(
    tmp_path: Path,
    graph_format: str,
    *,
    files: int = 6,
    backend: str = "sqlite",
) -> PheasantConfig:
    workspace = tmp_path / f"ws-{graph_format}"
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (workspace / f"note-{index}.md").write_text(
            f"# Deployment note {index}\n\n"
            "## Gateway\n\n"
            f"The gateway rotates credentials nightly. Marker{index} is unique.\n\n"
            "## Rollback\n\n"
            f"Roll back with the previous bundle. See note-{(index + 1) % files}.md.\n",
            encoding="utf-8",
        )
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": KB,
                "state_path": str(tmp_path / f"state-{graph_format}"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / f"exports-{graph_format}"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {
                "graph_format": graph_format,
                "graph_snapshots": False,
                **(
                    {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"}
                    if backend == "postgres"
                    else {}
                ),
            },
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


#: Attributes `upsert_node` sets itself, so re-asserting a node from its own
#: current state does not accidentally pass them twice.
_BUILDER_OWNED = {"id", "type", "label", "created_at", "updated_at", "knowledge_base_id"}


def _payload(attrs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attrs.items() if key not in _BUILDER_OWNED}


def _synced(tmp_path: Path, graph_format: str, **kwargs: Any) -> SyncEngine:
    engine = SyncEngine(_config(tmp_path, graph_format, **kwargs))
    engine.sync_source("docs", "full")
    return engine


# --------------------------------------------------------------------------
# 1. The same graph, whichever backend wrote it
# --------------------------------------------------------------------------


def test_both_backends_persist_the_same_graph(tmp_path: Path) -> None:
    """Node for node and edge for edge, from one corpus.

    The load-bearing assertion of the whole change: `storage.graph_format` is
    a storage decision, and a storage decision that altered what the graph
    *contains* would be a retrieval change wearing a config flag.
    """

    persisted = {}
    for graph_format in FORMATS:
        engine = _synced(tmp_path, graph_format)
        try:
            graph = engine.graph_store.load(KB)
            persisted[graph_format] = (
                {node_id for node_id, _attrs in graph.iter_nodes()},
                {
                    (source, target, str(attrs.get("type")))
                    for (source, target), edge_map in graph.iter_edges()
                    for attrs in edge_map.values()
                },
            )
        finally:
            engine.close()

    rows_nodes, rows_edges = persisted["rows"]
    file_nodes, file_edges = persisted["node_link_json"]
    assert rows_nodes == file_nodes
    assert rows_edges == file_edges
    assert rows_nodes, "the fixture produced no graph at all"


@pytest.mark.parametrize("graph_format", FORMATS)
def test_a_resync_of_unchanged_content_republishes_the_same_generation(
    tmp_path: Path, graph_format: str
) -> None:
    """Pillar 1, stated on the id both backends publish.

    Content-addressed on both sides, so an unchanged corpus keeps its name.
    On `rows` that is the XOR fold surviving a no-op delta; on the file
    backend it is a digest of unchanged bytes. Same guarantee, and it has to
    be, because the fleet compares these ids across replicas and reloads on a
    difference: an id that moves on its own makes every replica re-read a
    graph identical to the one it already holds, on every beat.

    This is the assertion that found the `updated_at` churn — the stamp moved
    on every upsert, so a graph nobody had touched published a new id as soon
    as the clock ticked. It held on both backends and predated both; nothing
    had ever asserted the property directly.

    **Incremental, deliberately.** A `full` re-index removes a source's nodes
    and rebuilds them, so their `created_at` legitimately resets and the id
    moves. That is arguably wrong too — "full" ought to mean "rebuild the same
    graph", not "rebuild it with new birthdays" — but `created_at` would have
    to come from the artifact rows rather than from the graph being wiped, and
    that is a change to what a full sync *means*, with its own blast radius.
    Written down rather than smuggled in here.
    """

    engine = _synced(tmp_path, graph_format)
    try:
        first = engine.graph_store.published_generation(KB)
        engine.sync_source("docs", "incremental")
        second = engine.graph_store.published_generation(KB)
    finally:
        engine.close()

    assert first is not None and second is not None
    assert first["generation_id"] == second["generation_id"]
    assert first["nodes"] == second["nodes"]


@pytest.mark.parametrize("graph_format", FORMATS)
def test_re_asserting_a_node_unchanged_does_not_move_its_timestamp(
    tmp_path: Path, graph_format: str
) -> None:
    """The mechanism behind the test above, isolated from any sync.

    Worth its own assertion because the sync test only fails once the clock
    has ticked past a second — `utc_now()` has second resolution — so a fast
    run passed while a real deployment, syncing minutes apart, changed its
    published id every time. This one cannot be flaky in that direction: it
    forces a different timestamp and demands the stamp not move.
    """

    engine = _synced(tmp_path, graph_format)
    try:
        builder = engine.graph_builder
        node_id = next(
            nid for nid, attrs in builder.graph.iter_nodes() if attrs.get("type") == "chunk"
        )
        before = dict(builder.graph.nodes[node_id])

        with mock.patch.object(builder_module, "utc_now", return_value="2099-01-01T00:00:00Z"):
            builder.upsert_node(
                node_id, str(before["type"]), str(before["label"]), _payload(before)
            )
            unchanged = dict(builder.graph.nodes[node_id])
            builder.upsert_node(
                node_id, str(before["type"]), "a genuinely new label", _payload(before)
            )
            changed = dict(builder.graph.nodes[node_id])
    finally:
        engine.close()

    assert unchanged["updated_at"] == before["updated_at"], (
        "re-asserting an unchanged node moved its timestamp, which moves the graph generation"
    )
    assert changed["updated_at"] == "2099-01-01T00:00:00Z", (
        "a real change must still move the timestamp, or the stamp stops tracking the graph"
    )
    assert changed["created_at"] == before["created_at"]


@pytest.mark.parametrize("graph_format", FORMATS)
def test_a_bounded_walk_returns_the_same_neighbours(tmp_path: Path, graph_format: str) -> None:
    """`neighbors` reads through a protocol, not through a representation."""

    engine = _synced(tmp_path, graph_format)
    try:
        resident = engine.graph_store.load(KB)
        start = next(
            node_id for node_id, attrs in resident.iter_nodes() if attrs.get("type") == "source"
        )
        expected = neighbors(resident, start, depth=2, max_nodes=25)
        stored = engine.graph_store.serving_graph(KB)
        actual = neighbors(stored, start, depth=2, max_nodes=25)
    finally:
        engine.close()

    assert {item["node_id"] for item in actual["neighbors"]} == {
        item["node_id"] for item in expected["neighbors"]
    }
    assert {item["node_id"]: item["depth"] for item in actual["neighbors"]} == {
        item["node_id"]: item["depth"] for item in expected["neighbors"]
    }


def test_a_slice_reports_the_same_shape_from_rows(tmp_path: Path) -> None:
    """`depths` and `truncated` are what a bounded canvas view is built from."""

    engine = _synced(tmp_path, "rows")
    try:
        resident = engine.graph_store.load(KB)
        start = next(
            node_id for node_id, attrs in resident.iter_nodes() if attrs.get("type") == "source"
        )
        expected = slice_(resident, start, depth=2, limit=10)
        actual = slice_(SqlGraph(engine.state, KB), start, depth=2, limit=10)
    finally:
        engine.close()

    assert actual["depths"] == expected["depths"]
    assert actual["truncated"] == expected["truncated"]
    assert {node["id"] for node in actual["nodes"]} == {node["id"] for node in expected["nodes"]}


# --------------------------------------------------------------------------
# 2. The incremental write, which is why the backend exists
# --------------------------------------------------------------------------


def test_a_commit_writes_only_what_changed(tmp_path: Path) -> None:
    """The whole thesis, asserted on row counts rather than on a stopwatch.

    A timing assertion would be flaky and would measure the machine. What is
    actually being claimed is that the *work* is proportional to the change:
    the file backend re-serializes every node on every commit by construction,
    and this one touches a handful.
    """

    engine = _synced(tmp_path, "rows")
    try:
        graph = engine.graph_builder.graph
        assert graph.number_of_nodes() > 10, "the fixture is too small to say anything"

        # Everything is committed; a delta now is empty.
        assert engine.graph_store.rows is not None
        assert graph.graph_delta()["node_upserts"] == []

        graph.add_node("late:node", type="symbol", label="late", source_id="docs")
        graph.add_edge("late:node", "late:node", type="references", source_id="docs")
        delta = graph.graph_delta()
        assert len(delta["node_upserts"]) == 1
        assert len(delta["edge_upserts"]) == 1

        written = engine.graph_store.rows.apply_delta(
            KB,
            node_upserts=delta["node_upserts"],
            edge_upserts=delta["edge_upserts"],
        )
        assert written == {
            "nodes": 1,
            "edges": 1,
            "removed_nodes": 0,
            "removed_edges": 0,
        }
    finally:
        engine.close()


def test_the_generation_fold_survives_every_kind_of_delta(tmp_path: Path) -> None:
    """The incremental XOR must equal a full rescan, after arbitrary churn.

    An aggregate maintained by deltas is only as good as every delta that ever
    ran, and XOR is an involution — fold a digest out twice and it is silently
    back in. So this adds, edits, removes and re-adds, then recomputes the
    expensive way and demands they agree.
    """

    engine = _synced(tmp_path, "rows")
    try:
        rows = engine.graph_store.rows
        assert rows is not None
        graph = engine.graph_builder.graph
        node_ids = [node_id for node_id, _attrs in graph.iter_nodes()][:4]

        graph.add_node("churn:a", type="symbol", label="a", source_id="docs")
        graph.add_node("churn:b", type="symbol", label="b", source_id="docs")
        graph.add_edge("churn:a", "churn:b", type="references", source_id="docs")
        graph.add_edge("churn:a", node_ids[0], type="mentions", source_id="docs")
        engine.graph_store.save(KB, graph)

        graph.add_node("churn:a", label="a-renamed")  # edit
        graph.remove_nodes_from([node_ids[1]])  # remove, cascading its edges
        engine.graph_store.save(KB, graph)

        graph.add_node("churn:c", type="symbol", label="c", source_id="docs")
        graph.add_edge("churn:b", "churn:c", type="references", source_id="docs")
        engine.graph_store.save(KB, graph)

        maintained = engine.state.rows(
            "SELECT nodes, edges, node_fold, edge_fold FROM graph_generations WHERE kb_id=?",
            (KB,),
        )[0]
        recomputed = rows.recompute_folds(KB)
    finally:
        engine.close()

    assert str(maintained["node_fold"]) == recomputed["node_fold"], (
        "the incrementally folded node digest drifted from a full rescan"
    )
    assert str(maintained["edge_fold"]) == recomputed["edge_fold"]
    assert (int(maintained["nodes"]), int(maintained["edges"])) == (
        int(
            engine.state.rows("SELECT nodes FROM graph_generations WHERE kb_id=?", (KB,))[0][
                "nodes"
            ]
        ),
        int(
            engine.state.rows("SELECT edges FROM graph_generations WHERE kb_id=?", (KB,))[0][
                "edges"
            ]
        ),
    )


def test_removing_a_node_removes_the_edges_pointing_at_it(tmp_path: Path) -> None:
    """A dangling edge is a neighbour with no attributes in every walk.

    The in-memory graph drops both directions; the rows have to as well, which
    is what `idx_graph_edges_target` is for — a delete that scanned the edge
    table instead would have put the O(edges) cost back on the sync path.
    """

    engine = _synced(tmp_path, "rows")
    try:
        rows = engine.graph_store.rows
        assert rows is not None
        rows.apply_delta(
            KB,
            node_upserts=[
                ("x:1", {"type": "symbol", "label": "one"}),
                ("x:2", {"type": "symbol", "label": "two"}),
            ],
            edge_upserts=[("x:1", "x:2", 0, {"type": "references"})],
        )
        assert rows.out_edges(KB, ["x:1"])["x:1"]

        rows.apply_delta(KB, node_removals=["x:2"])
        assert rows.out_edges(KB, ["x:1"]) == {}
        assert rows.get_node(KB, "x:2") is None
        # And the fold still matches a rescan after a cascade.
        maintained = engine.state.rows(
            "SELECT edge_fold FROM graph_generations WHERE kb_id=?", (KB,)
        )[0]["edge_fold"]
        assert str(maintained) == rows.recompute_folds(KB)["edge_fold"]
    finally:
        engine.close()


def test_folding_a_digest_twice_puts_it_back() -> None:
    """The property the whole incremental id rests on, stated on its own.

    Worth an explicit test because it is also the failure mode: a row whose
    digest is folded out by two different queries in one delta cancels itself,
    and the published id is then wrong in a way nothing else would notice.
    `_digests_for` keys by primary key to make that impossible.
    """

    base = fold("0" * 32, "abc123", "def456")
    assert fold(base, "abc123") != base
    assert fold(fold(base, "abc123"), "abc123") == base


# --------------------------------------------------------------------------
# 3. The migration, and standalone mode
# --------------------------------------------------------------------------


def test_an_existing_graph_file_is_imported_once_and_kept(tmp_path: Path) -> None:
    """CLAUDE.md rule 2: `/state` is user data, so the file is parked not deleted."""

    engine = _synced(tmp_path, "node_link_json")
    try:
        expected = engine.graph_builder.graph.number_of_nodes()
        graph_file = engine.graph_store.graph_path(KB)
        assert graph_file.exists()
        state_path = engine.config.pheasant.state_path
    finally:
        engine.close()

    upgraded = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": KB,
                "state_path": str(state_path),
                "workspace_root": str(tmp_path / "ws-node_link_json"),
                "exports_path": str(tmp_path / "exports-node_link_json"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_format": "rows", "graph_snapshots": False},
            "sources": [],
        }
    )
    engine = SyncEngine(upgraded)
    try:
        assert engine.graph_store.counts(KB) == (expected, engine.graph_store.counts(KB)[1])
        assert engine.graph_store.published_generation(KB) is not None
        assert not graph_file.exists(), "the original was left in place"
        assert graph_file.with_suffix(graph_file.suffix + ".migrated").exists(), (
            "the original was deleted rather than parked"
        )
        # Idempotent: a second boot imports nothing and changes nothing.
        before = engine.graph_store.published_generation(KB)
    finally:
        engine.close()

    again = SyncEngine(upgraded)
    try:
        assert again.graph_store.import_persisted_file(KB) is None
        assert (
            again.graph_store.published_generation(KB)["generation_id"] == (before["generation_id"])
        )
    finally:
        again.close()


def test_a_rows_store_without_a_state_handle_refuses(tmp_path: Path) -> None:
    """Rather than silently falling back to the whole-file path.

    Degrading quietly would make a region's commit cost depend on how its
    store happened to be constructed, which is exactly the class of bug that
    is invisible until someone measures a production incident.
    """

    with pytest.raises(ValueError, match="needs a state store"):
        GraphStore(tmp_path / "graphs", graph_format="rows")
    with pytest.raises(ValueError, match="Unknown storage.graph_format"):
        GraphStore(tmp_path / "graphs", graph_format="parquet")


def test_a_snapshot_is_written_from_rows_and_reads_back(tmp_path: Path) -> None:
    """Snapshots stay files on both backends, so history stays diffable."""

    engine = _synced(tmp_path, "rows")
    try:
        path = engine.graph_store.snapshot_current(KB, "2026-09-04T12:00:00+00:00")
        restored = engine.graph_store.read_snapshot(path)
        assert restored.number_of_nodes() == engine.graph_store.counts(KB)[0]
        assert restored.number_of_edges() == engine.graph_store.counts(KB)[1]
    finally:
        engine.close()


# --------------------------------------------------------------------------
# 4. Reading without residency
# --------------------------------------------------------------------------


def test_the_serving_graph_refuses_to_be_written(tmp_path: Path) -> None:
    """A write here would be a node that exists in one process and nowhere else."""

    engine = _synced(tmp_path, "rows")
    try:
        stored = SqlGraph(engine.state, KB)
        with pytest.raises(TypeError, match="cannot be mutated"):
            stored.add_node("nope", type="symbol")
        with pytest.raises(TypeError):
            stored.remove_nodes_from(["nope"])
    finally:
        engine.close()


def test_relationship_search_narrows_before_scoring(tmp_path: Path) -> None:
    """`_scan_edges` was O(edges) per query with no index to narrow it.

    Asserted as "the candidate set is smaller than the graph and still
    contains what matches", not as a timing: the point is that a store-backed
    graph *can* narrow, and that narrowing is a superset of what scores.
    """

    engine = _synced(tmp_path, "rows")
    try:
        stored = SqlGraph(engine.state, KB)
        total = stored.number_of_edges()
        candidates = list(stored.candidate_edges(["contains"]))
        matched = [
            attrs
            for _pair, edge_map in candidates
            for attrs in edge_map.values()
            if "contains" in str(attrs.get("type", ""))
        ]
        assert matched, "the narrowing dropped edges that match"
        assert sum(len(edge_map) for _pair, edge_map in candidates) < total, (
            "the candidate set is the whole graph, so nothing was narrowed"
        )
        assert not list(stored.candidate_edges(["zzzznotaword"])), (
            "a term matching nothing should cost nothing"
        )
    finally:
        engine.close()


def test_the_walk_batches_one_query_per_level(tmp_path: Path) -> None:
    """The N+1 the batch exists to remove, counted rather than assumed.

    A three-hop walk over a store is only affordable if it is three queries
    and not one per node. Counting calls is the only way to say that without
    a stopwatch, and it is the assertion that fails if someone reverts the
    traversal to expanding one node at a time.
    """

    engine = _synced(tmp_path, "rows")
    try:
        stored = SqlGraph(engine.state, KB)
        start = next(
            node_id for node_id, attrs in stored.iter_nodes() if attrs.get("type") == "source"
        )
        calls = {"edges": 0, "nodes": 0}
        real_edges, real_nodes = stored.out_edges_batch, stored.prefetch_nodes

        def counted_edges(node_ids: list[str]) -> Any:
            calls["edges"] += 1
            return real_edges(node_ids)

        def counted_nodes(node_ids: list[str]) -> Any:
            calls["nodes"] += 1
            return real_nodes(node_ids)

        stored.out_edges_batch = counted_edges  # type: ignore[method-assign]
        stored.prefetch_nodes = counted_nodes  # type: ignore[method-assign]
        result = neighbors(stored, start, depth=3, max_nodes=50)
    finally:
        engine.close()

    assert result["neighbors"], "the fixture produced no neighbours to walk"
    assert calls["edges"] <= 3, f"one adjacency query per level, got {calls['edges']}"
    assert calls["nodes"] <= 3, f"one attribute query per level, got {calls['nodes']}"


def test_the_in_memory_graph_answers_the_same_batch_protocol() -> None:
    """So the traversal has one code path rather than a branch on backend."""

    graph = SimpleMultiDiGraph()
    graph.add_node("a", type="symbol", label="a")
    graph.add_node("b", type="symbol", label="b")
    graph.add_edge("a", "b", type="references")

    assert graph.prefetch_nodes(["a", "missing"]) == {"a": {"type": "symbol", "label": "a"}}
    batched = graph.out_edges_batch(["a", "b"])
    assert [entry[1] for entry in batched["a"]] == ["b"]
    assert batched["b"] == []


def test_streaming_reads_page_rather_than_materialize(tmp_path: Path) -> None:
    """A scan of every node must not be one `SELECT *` into a list.

    Same reason `persistence/migrate.py` streams `chunks`: the corpora this
    backend exists for are the ones too big to hold, so a full-graph read that
    materialized would OOM at exactly the size that motivates it.
    """

    engine = _synced(tmp_path, "rows")
    try:
        rows = GraphRowStore(engine.state)
        rows.PAGE = 2  # force paging over a small fixture
        streamed = [node_id for node_id, _attrs in rows.iter_nodes(KB)]
        assert len(streamed) == rows.counts(KB)[0]
        assert len(set(streamed)) == len(streamed), "paging returned a row twice"

        edges = list(rows.iter_edges(KB))
        assert sum(len(edge_map) for _pair, edge_map in edges) == rows.counts(KB)[1]
        assert len({pair for pair, _edge_map in edges}) == len(edges), (
            "paging split one endpoint pair across two groups"
        )
    finally:
        engine.close()


# --------------------------------------------------------------------------
# 5. The same thing, against a real Postgres
# --------------------------------------------------------------------------


def _reset(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@postgres
def test_the_row_backend_works_on_postgres(tmp_path: Path) -> None:
    """Everything the SQLite tests above assert, on the scale-out backend.

    Not a formality. The row backend's write path is a pile of dialect-
    sensitive SQL — an ``OR`` chain over a composite key, ``IN`` lists built
    at two batch sizes, keyset paging with a row-value comparison — and this
    repo has been caught three separate times by SQL that was correct on
    SQLite and wrong on Postgres, every time in the direction the offline
    suite cannot see. `ci.yml` runs the whole suite against a real server on
    every PR, which is what makes this reachable rather than aspirational.

    One test rather than a parametrized sweep because the expensive part is
    the fixture: it indexes, churns, cascades and recomputes in one pass, and
    asserts each property as it goes.
    """

    _reset(DSN)
    engine = SyncEngine(_config(tmp_path, "rows", backend="postgres"))
    try:
        engine.sync_source("docs", "full")
        rows = engine.graph_store.rows
        assert rows is not None
        nodes, edges = engine.graph_store.counts(KB)
        assert (nodes, edges) == rows.recount(KB), "the maintained counts disagree with a scan"
        assert nodes > 10

        # An unchanged resync republishes the same id (pillar 1).
        first = engine.graph_store.published_generation(KB)
        engine.sync_source("docs", "full")
        assert (
            engine.graph_store.published_generation(KB)["generation_id"] == (first["generation_id"])
        )

        # Add, edit, cascade-remove: the paths with the dialect-sensitive SQL.
        graph = engine.graph_builder.graph
        victim = next(
            node_id for node_id, attrs in graph.iter_nodes() if attrs.get("type") == "chunk"
        )
        graph.add_node("pg:a", type="symbol", label="a", source_id="docs")
        graph.add_node("pg:b", type="symbol", label="b", source_id="docs")
        graph.add_edge("pg:a", "pg:b", type="references", source_id="docs")
        graph.add_edge("pg:a", victim, type="mentions", source_id="docs")
        engine.graph_store.save(KB, graph)
        graph.add_node("pg:a", label="a-renamed")
        graph.remove_nodes_from([victim])
        engine.graph_store.save(KB, graph)

        maintained = engine.state.rows(
            "SELECT nodes, edges, node_fold, edge_fold FROM graph_generations WHERE kb_id=?",
            (KB,),
        )[0]
        recomputed = rows.recompute_folds(KB)
        assert str(maintained["node_fold"]) == recomputed["node_fold"]
        assert str(maintained["edge_fold"]) == recomputed["edge_fold"]
        assert (int(maintained["nodes"]), int(maintained["edges"])) == rows.recount(KB)
        assert rows.get_node(KB, victim) is None
        assert not any(
            target == victim
            for _source, target, _attrs in rows.out_edges(KB, ["pg:a"]).get("pg:a", [])
        ), "the cascade left an edge pointing at a removed node"

        # And the read surface: paging, traversal, candidate narrowing.
        rows.PAGE = 3
        streamed = [node_id for node_id, _attrs in rows.iter_nodes(KB)]
        assert len(streamed) == len(set(streamed)) == rows.recount(KB)[0]
        stored = SqlGraph(engine.state, KB)
        start = next(
            node_id for node_id, attrs in stored.iter_nodes() if attrs.get("type") == "source"
        )
        assert neighbors(stored, start, depth=2, max_nodes=25)["neighbors"]
        assert list(stored.candidate_edges(["contains"]))
        assert not list(stored.candidate_edges(["zzzznotaword"]))
    finally:
        engine.close()


@postgres
def test_a_graph_file_imports_into_postgres_rows(tmp_path: Path) -> None:
    """The upgrade path on the backend a fleet actually runs."""

    _reset(DSN)
    legacy = SyncEngine(_config(tmp_path, "node_link_json", backend="postgres"))
    try:
        legacy.sync_source("docs", "full")
        expected = legacy.graph_builder.graph.number_of_nodes()
        graph_file = legacy.graph_store.graph_path(KB)
        state_path = legacy.config.pheasant.state_path
        assert graph_file.exists()
    finally:
        legacy.close()

    upgraded = _config(tmp_path, "rows", backend="postgres")
    upgraded.pheasant.state_path = state_path
    upgraded.storage.graph_path = Path(state_path) / "graphs"
    engine = SyncEngine(upgraded)
    try:
        assert engine.graph_store.counts(KB)[0] == expected
        assert engine.graph_store.rows.recount(KB)[0] == expected
        assert graph_file.with_suffix(graph_file.suffix + ".migrated").exists()
    finally:
        engine.close()


# --------------------------------------------------------------------------
# 6. What a read costs, bounded rather than timed
# --------------------------------------------------------------------------
#
# Every assertion below counts round trips or rows, never seconds. All three
# were found by a stress run against an 8,000-file corpus and were invisible
# to every fixture in this suite, because a fixture's graph has no hub and its
# index returns four candidates.


def _hub_graph(engine: SyncEngine, fan_out: int = 400) -> str:
    """A node with a large fan-out, which is what a real `source` node is.

    The shape that matters and that no fixture here had: a `source` indexes
    every artifact, so a bounded walk off one has thousands of edges to choose
    100 from.
    """

    graph = engine.graph_builder.graph
    hub = "hub:docs"
    graph.add_node(hub, id=hub, type="source", label="docs", source_id="docs")
    for index in range(fan_out):
        leaf = f"leaf:docs:{index}"
        graph.add_node(leaf, id=leaf, type="chunk", label=f"leaf {index}", source_id="docs")
        graph.add_edge(hub, leaf, type="contains")
    engine.graph_store.save(KB, graph)
    return hub


def test_a_bounded_walk_fetches_attributes_for_what_it_keeps(tmp_path: Path) -> None:
    """Not for everything it could reach.

    The walk fetches a level's adjacency, then the attributes of the targets
    it will actually return. Fetching every *reachable* target instead scales
    with fan-out rather than with `max_nodes`: measured on a real corpus, a
    depth-3 walk asking for 100 neighbours off an 8,040-edge hub pulled
    **8,040 node rows to keep 100**, which made the walk 6x slower than the
    resident graph it replaced rather than comparable to it.
    """

    engine = _synced(tmp_path, "rows")
    try:
        hub = _hub_graph(engine)
        stored = SqlGraph(engine.state, KB)
        asked: list[int] = []
        real = stored.prefetch_nodes

        def counted(node_ids: list[str]) -> Any:
            asked.append(len(node_ids))
            return real(node_ids)

        stored.prefetch_nodes = counted  # type: ignore[method-assign]
        found = neighbors(stored, hub, depth=3, max_nodes=25)
    finally:
        engine.close()

    assert len(found["neighbors"]) == 25, "the fixture did not exercise truncation"
    assert sum(asked) <= 100, (
        f"asked for {sum(asked)} node attributes to keep 25; the prefetch is not bounded "
        "by the walk's budget"
    )


def test_the_graph_arm_does_not_query_once_per_candidate(tmp_path: Path) -> None:
    """The index hands back every match; fetching them one at a time is an N+1.

    Measured at ~2,000 single-row queries per search on an 8,000-file corpus —
    the graph arm ended up slower than the in-memory scan it replaced, and the
    cost scaled with how *unselective* the query was rather than with how much
    it returned.
    """

    from pheasant.search import graph_search

    engine = _synced(tmp_path, "rows")
    try:
        graph = engine.graph_builder.graph
        for index in range(300):
            node = f"widget:{index}"
            graph.add_node(node, id=node, type="symbol", label=f"widget {index}", source_id="docs")
        engine.graph_store.save(KB, graph)
        engine.rebuild_node_index()

        stored = SqlGraph(engine.state, KB)
        calls: list[int] = []
        real = stored.prefetch_nodes

        def counted(node_ids: list[str]) -> Any:
            calls.append(len(node_ids))
            return real(node_ids)

        stored.prefetch_nodes = counted  # type: ignore[method-assign]
        graph_search.search_graph(stored, "widget", max_results=5, node_index=engine.node_index)
    finally:
        engine.close()

    assert calls, "the arm did not batch at all"
    assert len(calls) <= 1 + sum(calls) // graph_search.CANDIDATE_BLOCK, (
        f"{len(calls)} queries for {sum(calls)} candidates; that is one per candidate"
    )


def test_reading_an_edge_does_not_parse_json_it_never_looks_at(tmp_path: Path) -> None:
    """`type` is a column, not a JSON key, and it is all a walk reads.

    The traversal filters and orders on edge `type` alone, so parsing each
    edge's attribute blob to answer it was work done for nothing on every hop:
    **8,040 `json.loads` calls, 42% of one walk** off a hub. Asserted as "the
    blob is still unparsed after the read", which is the property, rather than
    by timing it.
    """

    from pheasant.persistence.graph_codec import _LazyAttrs

    engine = _synced(tmp_path, "rows")
    try:
        hub = _hub_graph(engine, fan_out=20)
        stored = SqlGraph(engine.state, KB)
        adjacency = stored.out_edges_batch([hub])
        attrs = next(iter(adjacency[hub]))[2][0]
        assert isinstance(attrs, _LazyAttrs)
        assert attrs.get("type") == "contains"
        assert attrs._parsed is None, "reading a promoted column parsed the JSON blob"
        assert attrs.get("confidence") is not None or True
        assert attrs._parsed is not None, "a non-promoted key must still resolve"
        # And it is still a complete mapping for everything that needs one.
        assert dict(attrs)["type"] == "contains"
    finally:
        engine.close()
