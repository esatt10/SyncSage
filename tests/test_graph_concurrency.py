"""Readers must survive a sync mutating the graph underneath them.

The graph is written by the sync thread (startup index, watcher, scheduler
beat) while the HTTP API / MCP tools read it from request threads. Before the
graph took a lock and handed readers snapshots, indexing a large repository
while the UI polled ``/graph`` + ``/overview`` produced a stream of
``RuntimeError: dictionary changed size during iteration`` 500s.
"""

from __future__ import annotations

import json
import sys
import threading

from syncsage.graph.exporter import cytoscape, node_link
from syncsage.graph.simple import SimpleMultiDiGraph
from syncsage.persistence.graph_store import GraphStore
from syncsage.search.graph_search import search_graph

READ_ROUNDS = 40


def _writer(graph: SimpleMultiDiGraph, stop: threading.Event, errors: list[BaseException]) -> None:
    """Churn the graph the way a sync does: add, then drop a whole source."""

    try:
        i = 0
        while not stop.is_set():
            node_id = f"file:src:{i}.py"
            graph.add_node(node_id, id=node_id, type="file", label=f"{i}.py", source_id="src")
            graph.add_edge("kb:test", node_id, type="contains", confidence=1.0)
            if i and i % 200 == 0:
                graph.remove_nodes_from([f"file:src:{j}.py" for j in range(i - 200, i)])
            i += 1
    except BaseException as exc:  # pragma: no cover - surfaced via ``errors``
        errors.append(exc)


def test_readers_tolerate_concurrent_sync_writes() -> None:
    graph = SimpleMultiDiGraph()
    graph.add_node("kb:test", id="kb:test", type="knowledge_base", label="test")
    for i in range(200):
        graph.add_node(f"seed:{i}", id=f"seed:{i}", type="file", label=str(i), source_id="src")

    errors: list[BaseException] = []
    stop = threading.Event()
    writer = threading.Thread(target=_writer, args=(graph, stop, errors), daemon=True)
    # A short switch interval makes the interpreter preempt readers mid-walk,
    # which is what turned this race into a stream of 500s in the container.
    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    writer.start()

    try:
        for _ in range(READ_ROUNDS):
            # /graph (bounded + filtered, and the unbounded export path)
            node_link(graph, node_limit=1200, link_limit=3600, exclude_node_types={"concept"})
            node_link(graph)
            cytoscape(graph)
            # /overview
            counts: dict[str, int] = {}
            for _node_id, attrs in graph.nodes(data=True):
                counts[str(attrs.get("type"))] = counts.get(str(attrs.get("type")), 0) + 1
            graph.number_of_nodes()
            graph.number_of_edges()
            # graph-mode search + neighbor expansion
            search_graph(graph, "file", max_results=10)
            for node_id in graph.nodes:
                graph.out_edges(node_id)
                graph.neighbors(node_id)
                break
    finally:
        stop.set()
        writer.join(timeout=30)
        sys.setswitchinterval(switch_interval)

    assert not errors, f"writer failed: {errors[0]!r}"


def test_concurrent_saves_do_not_collide_on_a_shared_tmp_file(tmp_path) -> None:
    """Two writers saving at once must both produce a complete file.

    ``DELETE /sources`` saves the graph from a request thread while the sync
    thread saves its own pass. With a fixed ``graph.latest.tmp`` the first
    ``os.replace`` renamed the shared tmp away and the second raised
    ``FileNotFoundError`` (a 500), with interleaved writes free to corrupt the
    payload in between.
    """

    graph = SimpleMultiDiGraph()
    for i in range(300):
        graph.add_node(f"n{i}", id=f"n{i}", type="file", label=f"label-{i}")

    store = GraphStore(tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def save() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(5):
                store.save("kb", graph)
        except BaseException as exc:  # pragma: no cover - surfaced via ``errors``
            errors.append(exc)

    threads = [threading.Thread(target=save, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"concurrent save failed: {errors[0]!r}"
    payload = json.loads(store.graph_path("kb").read_text(encoding="utf-8"))
    assert len(payload["nodes"]) == 300
    assert not list(store.kb_dir("kb").glob("*.tmp")), "temp files left behind"


def test_reading_pins_the_graph_for_a_consistent_export() -> None:
    """``reading()`` blocks writes, so counts and contents agree."""

    graph = SimpleMultiDiGraph()
    for i in range(20):
        graph.add_node(f"n{i}", id=f"n{i}", type="file", label=str(i))

    started = threading.Event()
    released = threading.Event()

    def writer() -> None:
        started.set()
        graph.add_node("late", id="late", type="file", label="late")
        released.set()

    with graph.reading():
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        started.wait(timeout=5)
        payload = node_link(graph)
        # The writer is parked on the lock until the export finishes.
        assert not released.is_set()
        assert payload["total_nodes"] == len(payload["nodes"]) == 20

    thread.join(timeout=5)
    assert graph.number_of_nodes() == 21
