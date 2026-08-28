"""The log tier: its own queue, its own worker, its own storage tiers.

The tier exists to keep observation off two hot paths -- the request, and the
indexer's ``sync_lock``. These tests pin the parts of that which are easy to
regress silently:

* the index queue's own SQL did not move when it was parameterized;
* the two queues cannot claim, dead-letter, or drain each other's work;
* a redelivered batch is a no-op, so at-least-once delivery is safe;
* a roll is bounded, and cold storage is readable by something that is not
  pheasant.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.state_store import StateStore
from pheasant.sync.log_queue import (
    DEFAULT_LOG_MAX_ATTEMPTS,
    LogQueue,
    LogTask,
    drop_expired_partitions,
    handle_batch,
    hot_row_count,
    ingest_spool,
    log_queue_from_config,
    roll,
    run_log_maintenance,
    write_events,
)
from pheasant.sync.queue import DEAD, DONE, PENDING, IndexTask, LocalQueue, drain
from pheasant.telemetry.interactions import InteractionEvent, QueueSink, SpoolSink


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "p.db")
    store.migrate()
    return store


def _event(index: int, *, when: str = "2026-01-01T00:00:00.000000Z") -> InteractionEvent:
    return InteractionEvent(
        kb_id="kb",
        operation="search_context",
        trace_id=f"{index:032x}",
        span_id=f"{index:016x}",
        started_at=when,
        session_id=f"s{index % 3}",
        query_text=f"query {index}",
    )


# --------------------------------------------------------------------------
# The parameterization did not move the index queue
# --------------------------------------------------------------------------


def test_the_index_queues_own_sql_is_unchanged_by_the_parameterization() -> None:
    """`LocalQueue` gained a table seam so the log tier could reuse its
    race-free claim rather than copy it. The index path must not have moved:
    every predicate in the claim is still a literal, and the column lists
    still name exactly what they named."""

    queue = LocalQueue.__new__(LocalQueue)

    publish, claim = queue._publish_sql(), queue._claim_sql()

    assert publish.startswith(
        "INSERT INTO index_tasks(id, source_id, mode, payload, status, attempts, "
        "max_attempts, owner, visible_at, enqueued_at, updated_at, last_error) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
    )
    assert publish.endswith("WHERE index_tasks.status=?")
    assert claim.endswith("RETURNING id, source_id, mode, payload, attempts, max_attempts")
    # The whole race argument, still spelled out. The visibility check appears
    # twice on purpose -- once picking the row, once *outside* the subquery --
    # and it is the outer one that makes the loser of a contended claim match
    # nothing, because the winner has just pushed `visible_at` into the future.
    assert claim.count("visible_at<=?") == 2
    assert "AND status IN (?,?) AND visible_at<=? RETURNING" in claim


def test_the_log_queue_is_a_different_table_with_no_indexing_vocabulary() -> None:
    queue = LogQueue.__new__(LogQueue)

    assert LogQueue.TABLE == "log_tasks"
    assert "index_tasks" not in queue._publish_sql()
    assert "index_tasks" not in queue._claim_sql()
    # No source_id, no mode: a batch is opaque and the payload is the task.
    assert "source_id" not in queue._publish_sql()
    assert queue._claim_sql().endswith("RETURNING id, payload, attempts, max_attempts")


# --------------------------------------------------------------------------
# Isolation between the two queues
# --------------------------------------------------------------------------


def test_the_two_queues_cannot_claim_each_others_work(state: StateStore) -> None:
    """Sharing one table would mean two handlers stealing from each other --
    the reason this is a second table and not a `kind` column."""

    index_queue, log_queue = LocalQueue(state), LogQueue(state)
    index_queue.publish(IndexTask(id="idx-1", source_id="docs"))
    log_queue.publish(LogTask(id="log-1", payload={"events": []}))

    assert index_queue.claim("a").id == "idx-1"
    assert index_queue.claim("a") is None

    assert log_queue.claim("b").id == "log-1"
    assert log_queue.claim("b") is None


def test_a_dead_lettered_batch_does_not_show_up_in_the_index_queues_depth(
    state: StateStore,
) -> None:
    index_queue, log_queue = LocalQueue(state), LogQueue(state)
    log_queue.publish(LogTask(id="log-1", payload={}, max_attempts=1))
    claimed = log_queue.claim("b")
    log_queue.nack(claimed, "poison")

    assert log_queue.depth()[DEAD] == 1
    assert index_queue.depth()[DEAD] == 0
    assert log_queue.requeue_dead() == 1
    assert log_queue.depth()[PENDING] == 1


def test_a_batch_is_best_effort_and_gives_up_sooner_than_a_sync() -> None:
    """Retrying a poisoned batch three times costs more than the data."""

    assert DEFAULT_LOG_MAX_ATTEMPTS < 3


# --------------------------------------------------------------------------
# Publish -> claim -> persist
# --------------------------------------------------------------------------


def test_a_batch_survives_the_process_that_produced_it(state: StateStore) -> None:
    queue = LogQueue(state)
    QueueSink(queue, kb_id="kb").write([_event(1), _event(2)])

    written = drain(queue, lambda task: handle_batch(state, task), owner="logger-1")

    assert written == [2]
    assert hot_row_count(state) == 2
    assert queue.depth()[DONE] == 1


def test_redelivering_a_batch_writes_no_new_rows(state: StateStore) -> None:
    """At-least-once is the queue's normal mode. A double-count here would
    inflate a formation threshold -- a wrong memory, not a wrong number."""

    queue = LogQueue(state)
    QueueSink(queue, kb_id="kb").write([_event(1), _event(2)])
    payload = json.loads(state.rows("SELECT payload FROM log_tasks", ())[0]["payload"])

    handle_batch(state, LogTask(id="x", payload=payload))
    handle_batch(state, LogTask(id="x", payload=payload))

    assert hot_row_count(state) == 2


def test_one_malformed_event_does_not_cost_the_whole_batch(state: StateStore) -> None:
    """A batch is inserted in one transaction, so an event with a null
    ``trace_id`` -- a truncated spool line, a garbled payload -- used to raise
    IntegrityError and roll back every good event beside it. The batch then
    nacked, retried, failed identically and dead-lettered: one bad line for
    hundreds of good observations."""

    good = [_event(1).as_json(), _event(2).as_json()]
    payload = {
        "events": [
            good[0],
            "not-an-event",
            {"trace_id": None, "span_id": None},
            {"kb_id": "kb", "operation": "x"},  # no trace/span/started_at
            good[1],
        ]
    }

    written = handle_batch(state, LogTask(id="x", payload=payload))

    assert written == 2
    assert hot_row_count(state) == 2


def test_an_unwritable_event_is_recognised_before_it_reaches_sql() -> None:
    assert _event(1).is_writable
    assert not InteractionEvent(kb_id="kb", operation="x").is_writable
    assert not InteractionEvent(
        kb_id="", operation="x", trace_id="a", span_id="b", started_at="t"
    ).is_writable


# --------------------------------------------------------------------------
# Hot -> cold
# --------------------------------------------------------------------------


def _age_everything(state: StateStore) -> None:
    state.execute("UPDATE interaction_events SET started_at=?", ("2020-03-04T05:06:07.000000Z",))


def test_a_roll_is_bounded_so_it_cannot_stall_the_beat_that_runs_it(
    state: StateStore, tmp_path: Path
) -> None:
    """In one container this runs on the scheduler beat. An unbounded roll
    there is an unbounded stall for every source in the region."""

    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 0
    settings.max_rows_per_pass = 3
    write_events(state, [_event(index) for index in range(10)])
    _age_everything(state)

    first = roll(state, settings, exports_path=tmp_path)

    assert first["rolled"] == 3
    assert hot_row_count(state) == 7


def test_cold_storage_is_parquet_a_stranger_can_read(state: StateStore, tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 0
    settings.cold_enabled = True
    write_events(state, [_event(index) for index in range(5)])
    _age_everything(state)

    report = roll(state, settings, exports_path=tmp_path)

    assert report["rolled"] == 5
    assert report["disposition"] == "cold"
    assert hot_row_count(state) == 0
    # Partitioned by day, so retention can drop whole directories.
    partitions = list((tmp_path / "interactions").glob("dt=*"))
    assert [p.name for p in partitions] == ["dt=2020-03-04"]

    rows = (
        duckdb.connect(":memory:")
        .execute(
            "SELECT count(*), count(DISTINCT session_id) FROM "
            f"read_parquet('{tmp_path}/interactions/**/*.parquet')"
        )
        .fetchone()
    )
    assert rows == (5, 3)


def test_without_cold_storage_expired_rows_are_simply_dropped(
    state: StateStore, tmp_path: Path
) -> None:
    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 0
    settings.cold_enabled = False
    write_events(state, [_event(index) for index in range(4)])
    _age_everything(state)

    report = roll(state, settings, exports_path=tmp_path)

    assert report["disposition"] == "dropped"
    assert hot_row_count(state) == 0
    assert not (tmp_path / "interactions").exists()


def test_retention_keeps_rows_inside_the_window(state: StateStore, tmp_path: Path) -> None:
    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 7
    fresh = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    write_events(state, [_event(1, when=fresh), _event(2)])

    roll(state, settings, exports_path=tmp_path)

    assert hot_row_count(state) == 1


def test_expired_partitions_are_dropped_whole(tmp_path: Path) -> None:
    """Whole days, never individual rows: a partition is the unit cold
    storage is written in."""

    settings = PheasantConfig().observability.interactions
    settings.cold_retention_days = 30
    root = tmp_path / "interactions"
    old = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%d")
    new = datetime.now(UTC).strftime("%Y-%m-%d")
    for day in (old, new):
        (root / f"dt={day}").mkdir(parents=True)
        (root / f"dt={day}" / "part.parquet").write_bytes(b"")

    dropped = drop_expired_partitions(tmp_path, settings)

    assert dropped == [old]
    assert not (root / f"dt={old}").exists()
    assert (root / f"dt={new}").exists()


def test_null_cold_retention_keeps_everything_forever(tmp_path: Path) -> None:
    settings = PheasantConfig().observability.interactions
    assert settings.cold_retention_days is None
    (tmp_path / "interactions" / "dt=1999-01-01").mkdir(parents=True)

    assert drop_expired_partitions(tmp_path, settings) == []
    assert (tmp_path / "interactions" / "dt=1999-01-01").exists()


# --------------------------------------------------------------------------
# The spool
# --------------------------------------------------------------------------


def test_a_spool_is_ingested_once_and_then_removed(state: StateStore, tmp_path: Path) -> None:
    SpoolSink(tmp_path / "spool", owner="api-1").write([_event(1), _event(2)])

    assert ingest_spool(state, tmp_path / "spool") == 2
    assert hot_row_count(state) == 2
    assert not list((tmp_path / "spool").rglob("*.ndjson"))
    assert ingest_spool(state, tmp_path / "spool") == 0


def test_a_truncated_spool_line_does_not_strand_the_batch_behind_it(
    state: StateStore, tmp_path: Path
) -> None:
    """A spool is written by a process that may have been killed mid-line."""

    spool = tmp_path / "spool" / "api-1"
    spool.mkdir(parents=True)
    good = json.dumps(_event(1).as_json())
    (spool / "2026-01-01.ndjson").write_text(f'{good}\n{{"trace_id": "trunc\n')

    assert ingest_spool(state, tmp_path / "spool") == 1


# --------------------------------------------------------------------------
# Rule 7 — nothing happens when nothing is enabled
# --------------------------------------------------------------------------


def test_no_log_queue_by_default(state: StateStore) -> None:
    assert log_queue_from_config(PheasantConfig(), state) is None


def test_a_queue_needs_observation_on_not_just_the_queue_flag(state: StateStore) -> None:
    """Otherwise a region publishes batches nothing will ever produce."""

    config = PheasantConfig.model_validate(
        {"observability": {"interactions": {"enabled": False, "queue": {"enabled": True}}}}
    )
    assert log_queue_from_config(config, state) is None


def test_maintenance_no_ops_fast_when_observation_is_off(state: StateStore) -> None:
    assert run_log_maintenance(state, PheasantConfig()) is None


def test_an_unknown_backend_falls_back_rather_than_failing_startup(state: StateStore) -> None:
    config = PheasantConfig.model_validate(
        {
            "observability": {
                "interactions": {"enabled": True, "queue": {"enabled": True, "backend": "kafka"}}
            }
        }
    )
    assert isinstance(log_queue_from_config(config, state), LogQueue)


# --------------------------------------------------------------------------
# Postgres: where the parameterized claim is actually contended
# --------------------------------------------------------------------------

POSTGRES_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the log-queue race test",
)


def _pg_stores() -> tuple[Any, list[Any]]:
    from pheasant.persistence.backends import PostgresBackend

    stores: list[Any] = []

    def new_store() -> StateStore:
        store = StateStore(backend=PostgresBackend(POSTGRES_DSN, pool_size=6))
        stores.append(store)
        return store

    return new_store, stores


@postgres
def test_the_log_claim_is_race_free_on_postgres() -> None:
    """The claim was parameterized by table so the log tier could reuse it
    rather than copy it. SQLite cannot tell whether that broke anything --
    one writer at a time means two claimants are serialized by the file lock
    and every guard is redundant. Postgres runs them concurrently, and under
    READ COMMITTED the loser's UPDATE re-evaluates its WHERE after the winner
    commits, which is what the outer `status`/`visible_at` clauses are for.

    The sibling of `test_the_claim_statement_is_race_free_on_postgres`, and
    the reason the two queues share one implementation of this statement.
    """

    pytest.importorskip("psycopg", reason="the [postgres] extra is optional")
    new_store, stores = _pg_stores()

    seed = new_store()
    seed.migrate()
    seed.execute("DELETE FROM log_tasks", ())
    queue = LogQueue(seed)
    for index in range(20):
        queue.publish(LogTask(id=f"pg-log-{index}", payload={"events": []}))

    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker(name: str) -> None:
        own = LogQueue(new_store())
        barrier.wait(timeout=15)
        while True:
            task = own.claim(name, visibility_seconds=300.0)
            if task is None:
                return
            with lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker, args=(f"pg-l{index}",)) for index in range(5)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert sorted(claimed) == sorted(f"pg-log-{index}" for index in range(20))
        assert len(claimed) == len(set(claimed)), "two workers claimed the same batch"
    finally:
        for store in stores:
            store.close()


@postgres
def test_the_two_queues_do_not_contend_on_postgres() -> None:
    """Separate tables, so an indexer draining sources and a logger draining
    batches never see each other's rows -- the isolation this is a second
    table for."""

    pytest.importorskip("psycopg", reason="the [postgres] extra is optional")
    new_store, stores = _pg_stores()
    try:
        seed = new_store()
        seed.migrate()
        seed.execute("DELETE FROM log_tasks", ())
        seed.execute("DELETE FROM index_tasks", ())

        index_queue, log_queue = LocalQueue(seed), LogQueue(seed)
        index_queue.publish(IndexTask(id="idx-a", source_id="docs"))
        log_queue.publish(LogTask(id="log-a", payload={"events": []}))

        assert index_queue.claim("i").id == "idx-a"
        assert index_queue.claim("i") is None
        assert log_queue.claim("l").id == "log-a"
        assert log_queue.claim("l") is None
    finally:
        for store in stores:
            store.close()


@postgres
def test_the_ledger_round_trips_on_postgres() -> None:
    """Three shapes this repo has already been bitten by, all on this path:
    a declared FK a maintenance path violates, a discarded `cursor.rowcount`,
    and `INSERT OR IGNORE` where `ON CONFLICT DO NOTHING` is required. The
    ledger's idempotent insert and its bounded chunked delete exercise the
    last two directly."""

    pytest.importorskip("psycopg", reason="the [postgres] extra is optional")
    new_store, stores = _pg_stores()
    try:
        store = new_store()
        store.migrate()
        store.execute("DELETE FROM interaction_events", ())

        events = [_event(index) for index in range(1200)]
        assert write_events(store, events) == 1200
        # Redelivery: the portable ON CONFLICT form, not SQLite's INSERT OR
        # IGNORE, which Postgres does not have at all.
        assert write_events(store, events) == 1200
        assert hot_row_count(store) == 1200

        settings = PheasantConfig().observability.interactions
        settings.hot_retention_days = 0
        store.execute(
            "UPDATE interaction_events SET started_at=?", ("2020-03-04T05:06:07.000000Z",)
        )
        # Crosses the 500-row delete chunk boundary several times.
        report = roll(store, settings, exports_path=Path("/tmp"))
        assert report["rolled"] == 1200
        assert hot_row_count(store) == 0
    finally:
        for store in stores:
            store.close()


@postgres
def test_the_new_tables_are_in_the_postgres_schema_probe() -> None:
    """`_migrate_postgres` skips DDL when a marker says the schema is current.
    A table absent from the `required` probe is a table a stale marker can
    skip -- which is why index_tasks, source_leases and sync_fingerprints are
    all listed there."""

    pytest.importorskip("psycopg", reason="the [postgres] extra is optional")
    import inspect

    from pheasant.persistence.state_store import StateStore as Store

    source = inspect.getsource(Store._migrate_postgres)
    assert '"log_tasks"' in source
    assert '"interaction_events"' in source

    new_store, stores = _pg_stores()
    try:
        store = new_store()
        store.migrate()
        # Replaying is idempotent and leaves both tables usable.
        store.migrate()
        for table in ("log_tasks", "interaction_events"):
            assert store.backend.table_columns(table)
    finally:
        for store in stores:
            store.close()
