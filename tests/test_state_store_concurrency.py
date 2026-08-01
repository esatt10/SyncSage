"""StateStore reads must be safe AND genuinely concurrent under load.

Regression test for a real bug, and for the fix's own performance cost.

Round 1 (correctness): the agentic assistant workflow runs nested thread
pools (multi_search's per-query pool, containing hybrid.py's per-search-mode
pool), and every search arm (text/vector/graph) reads through
`StateStore.rows()`. The original bug shared one `sqlite3.Connection` across
every thread; without serializing execute()+fetch as one atomic unit,
concurrent queries of DIFFERENT shapes interleaved cursor state and handed
back rows missing expected columns -- reproduced live as
`IndexError: tuple index out of range` from `row["chunk_id"]` inside
vector_store.search(), which silently downgraded the agentic workflow to the
simple one on every request.

The first fix (a global lock around `rows()`) was correct but serialized
every read, which measurably hurt latency: an agentic retrieve step fanning
out 4 queries across 3 search modes went from embarrassingly parallel to
mostly serial (measured live: 22-29s for that step, vs 1.3-2s for a
single-query retrieve doing comparable work). The real fix is a connection
per thread -- SQLite's WAL mode is explicitly designed for concurrent readers
under that pattern -- which removes the shared cursor state instead of
locking around it, so both correctness *and* concurrency hold at once.
"""

from __future__ import annotations

import threading
import time

from syncsage.persistence.state_store import StateStore

QUERIES = [
    ("SELECT ? AS a, ? AS b, ? AS c", (1, 2, 3), {"a", "b", "c"}),
    ("SELECT ? AS x, ? AS y", (10, 20), {"x", "y"}),
    (
        "SELECT ? AS chunk_id, ? AS source_id, ? AS artifact_id",
        (1, 2, 3),
        {"chunk_id", "source_id", "artifact_id"},
    ),
    ("SELECT ? AS one", (1,), {"one"}),
]


def test_concurrent_rows_calls_never_return_a_row_with_the_wrong_shape(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.migrate()

    errors: list[BaseException] = []
    barrier = threading.Barrier(len(QUERIES) * 6)

    def worker(sql: str, params: tuple, expected_cols: set[str]) -> None:
        barrier.wait(timeout=10)
        for _ in range(40):
            try:
                rows = store.rows(sql, params)
                for row in rows:
                    # This is exactly the access pattern that raised
                    # IndexError under the unsynchronized-connection bug: a
                    # column lookup on a Row whose cursor state got
                    # interleaved with a concurrently-running different query.
                    actual_cols = set(row.keys())
                    if actual_cols != expected_cols:
                        errors.append(
                            AssertionError(
                                f"row shape corrupted: expected {expected_cols}, "
                                f"got {actual_cols} for query {sql!r}"
                            )
                        )
            except BaseException as exc:  # noqa: BLE001 - capture everything, incl. IndexError
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(sql, params, cols))
        for sql, params, cols in QUERIES
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"{len(errors)} concurrency failures, first: {errors[0]!r}"
    store.close()


def test_each_thread_gets_its_own_connection(tmp_path) -> None:
    """The structural guarantee the correctness fix relies on."""

    store = StateStore(tmp_path / "state.db")
    store.migrate()

    seen: dict[int, int] = {}
    lock = threading.Lock()

    def worker() -> None:
        conn_id = id(store.conn)
        with lock:
            seen[threading.get_ident()] = conn_id

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(threads) == len(seen)
    assert len(set(seen.values())) == len(seen), "two threads shared one connection"
    store.close()


def test_a_slow_read_does_not_block_a_concurrent_read(tmp_path) -> None:
    """The property the connection-per-thread fix exists to restore.

    A wall-clock speedup microbenchmark is a poor test here: for a query that
    returns many rows, most of the elapsed time is Python-side Row
    construction, which the GIL serializes no matter how many SQLite
    connections exist -- confirmed by hand, that version of this test failed
    even against the fixed code. What actually distinguishes "one global lock
    around every read" from "one connection per thread" is whether an
    unrelated thread's slow work can stall a different thread's fast read.
    `time.sleep()` is a documented GIL-releasing call, so registering it as a
    SQL function makes "slow" mean genuinely blocked-and-yielding, not
    CPU-bound -- the same shape as the real bottleneck (an embedding provider
    round trip, or a slow query) blocking search arms that share nothing with
    it.
    """

    store = StateStore(tmp_path / "state.db")
    store.migrate()

    slow_started = threading.Event()
    slow_done = threading.Event()
    fast_finished_while_slow_still_running = threading.Event()

    def slow_worker() -> None:
        conn = store.conn  # this thread's own connection
        conn.create_function("slow_sleep", 1, lambda seconds: time.sleep(seconds) or 0)
        slow_started.set()
        store.rows("SELECT slow_sleep(?)", (1.0,))
        slow_done.set()

    def fast_worker() -> None:
        slow_started.wait(timeout=5)
        store.rows("SELECT 1")
        if not slow_done.is_set():
            fast_finished_while_slow_still_running.set()

    slow = threading.Thread(target=slow_worker)
    fast = threading.Thread(target=fast_worker)
    slow.start()
    fast.start()
    slow.join(timeout=10)
    fast.join(timeout=10)

    assert fast_finished_while_slow_still_running.is_set(), (
        "a fast read waited for an unrelated slow read on another thread -- "
        "reads are still serialized"
    )
    store.close()
