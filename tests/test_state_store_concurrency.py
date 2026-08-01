"""StateStore.rows() must be safe under concurrent access from multiple
threads sharing one connection.

Regression test for a real bug: the agentic assistant workflow runs nested
thread pools (multi_search's per-query pool, containing hybrid.py's
per-search-mode pool), and every search arm (text/vector/graph) reads through
`StateStore.rows()`, which shares a single `sqlite3.Connection` across all of
them. Without serializing execute()+fetch as one atomic unit, concurrent
queries of DIFFERENT shapes interleaved cursor state and handed back rows
missing expected columns -- reproduced live as
`IndexError: tuple index out of range` from `row["chunk_id"]` inside
vector_store.search(), which silently downgraded the agentic workflow to the
simple one on every request.
"""

from __future__ import annotations

import threading

from syncsage.persistence.state_store import StateStore

QUERIES = [
    ("SELECT ? AS a, ? AS b, ? AS c", (1, 2, 3), {"a", "b", "c"}),
    ("SELECT ? AS x, ? AS y", (10, 20), {"x", "y"}),
    ("SELECT ? AS chunk_id, ? AS source_id, ? AS artifact_id", (1, 2, 3), {"chunk_id", "source_id", "artifact_id"}),
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
