"""Acceptance tests for per-region vector self-search (Synapse step 21.4).

Fully offline: embeddings come from the deterministic ``StubEmbedder``
(planted synonym ``automobile -> car``) and the OpenAI-spec wire format is
exercised against a monkeypatched ``urlopen``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_vector_engine, run_sync


def _search(engine: Any, query: str, mode: str, source_name: str | None = None) -> dict:
    from syncsage.search.hybrid import HybridSearch
    from syncsage.search.sqlite_store import SearchStore

    searcher = HybridSearch(SearchStore(engine.state), vector=engine.vector_searcher())
    return searcher.search_context(
        engine.config.knowledge_base_id,
        query,
        mode=mode,
        max_results=5,
        source_name=source_name,
    )


def _result_paths(payload: dict) -> list[str]:
    return [str(item.get("relative_path") or "") for item in payload["results"]]


def test_vector_and_hybrid_surface_lexically_absent_synonym(tmp_path: Path) -> None:
    """Query "car" finds the automobile chunk via vectors; text mode misses it."""

    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")

    text = _search(engine, "car", "text")
    assert all("vehicles.md" not in path for path in _result_paths(text))

    vector = _search(engine, "car", "vector")
    assert vector["counts"]["vector"] >= 1
    assert _result_paths(vector), "Expected vector candidates for the synonym query."
    assert "vehicles.md" in _result_paths(vector)[0]
    top = vector["results"][0]
    # Same result shape as text hits: path/preview/score/node ids.
    assert top["type"] == "chunk"
    assert top["reason"] == "Vector similarity"
    assert top["node_id"] and top["chunk_id"]
    assert 0.0 <= top["score"] <= 1.0
    assert top["chunks"][0]["text_preview"]
    assert top["provenance"]["source_id"] == "notes"

    hybrid = _search(engine, "car", "hybrid")
    assert any("vehicles.md" in path for path in _result_paths(hybrid))


def test_vector_mode_respects_source_filter(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    payload = _search(engine, "car", "vector", source_name="no-such-source")
    assert payload["results"] == []


def test_changed_chunk_reembeds_only_that_chunk(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    embedder = engine.vectors.embedder
    store = engine.vectors.store
    baseline_texts = embedder.texts_embedded
    baseline_count = store.count()

    notes_dir = Path(engine.config.sources[0].path)
    kitchen = notes_dir / "kitchen.md"
    kitchen.write_text(
        kitchen.read_text(encoding="utf-8") + "\nPolish the silverware weekly.\n",
        encoding="utf-8",
    )
    run_sync(engine, source_name="notes", mode="incremental")

    # Only the changed file's single chunk was re-embedded; the stale vector
    # for its previous text_hash was pruned, so the store count is unchanged.
    assert embedder.texts_embedded - baseline_texts == 1
    assert store.count() == baseline_count


def test_removed_artifact_vectors_are_deleted(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    store = engine.vectors.store
    before = store.count()
    assert before >= 2

    notes_dir = Path(engine.config.sources[0].path)
    (notes_dir / "kitchen.md").unlink()
    result = run_sync(engine, source_name="notes", mode="full")

    assert store.count() == before - 1
    assert all("kitchen.md" not in chunk_id for chunk_id in store.source_chunk_ids("notes"))
    assert result.details["pruned_vectors"] >= 1


def test_disabled_embeddings_change_nothing(
    loaded_config: Any, sync_engine: Any, state_path: Path
) -> None:
    """Default config: no vector dir, no embedder, search modes as before."""

    run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    assert sync_engine.vectors is None
    assert sync_engine.vector_searcher() is None
    assert not (state_path / "vectors").exists()

    text = _search(sync_engine, "sync engine", "text")
    assert text["results"]
    assert "vector" not in {item["reason"] for item in text["results"]}
    vector = _search(sync_engine, "sync engine", "vector")
    assert vector["results"] == []
    assert vector["counts"]["vector"] == 0
    hybrid = _search(sync_engine, "sync engine", "hybrid")
    assert hybrid["results"]


def test_stub_embedder_is_deterministic_and_synonym_aligned() -> None:
    from syncsage.search.vector_store import StubEmbedder

    first = StubEmbedder(dim=32)
    second = StubEmbedder(dim=32)
    assert first.embed(["the automobile fleet"]) == second.embed(["the automobile fleet"])

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        return dot / norm

    car = first.embed(["car"])[0]
    automobile = first.embed(["automobile"])[0]
    pantry = first.embed(["pantry"])[0]
    assert _cosine(car, automobile) > 0.99
    assert _cosine(car, automobile) > _cosine(car, pantry) + 0.5


def test_openai_spec_embedder_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline conformance with the OpenAI embeddings HTTP shape (x-repo pin)."""

    from syncsage.search.vector_store import OpenAISpecEmbedder

    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.get_header("Authorization")
        count = len(captured["body"]["input"])
        # Deliberately out of order: the client must sort by data[i].index.
        return _FakeResponse(
            {"data": [{"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(count))]}
        )

    monkeypatch.setattr("syncsage.search.vector_store.urlopen", fake_urlopen)
    monkeypatch.setenv("SYNCSAGE_TEST_EMBED_KEY", "sekrit")
    embedder = OpenAISpecEmbedder(
        "http://localhost:9/v1/",
        "fleet-pinned-model",
        api_key_env="SYNCSAGE_TEST_EMBED_KEY",
        dimensions=2,
        batch_size=8,
    )
    vectors = embedder.embed(["alpha", "beta"])

    assert captured["url"] == "http://localhost:9/v1/embeddings"
    assert captured["body"]["model"] == "fleet-pinned-model"
    assert captured["body"]["input"] == ["alpha", "beta"]
    assert captured["body"]["dimensions"] == 2
    assert captured["auth"] == "Bearer sekrit"
    assert vectors == [[0.0, 1.0], [1.0, 1.0]]
    assert embedder.calls == 1
    assert embedder.texts_embedded == 2


@pytest.fixture(params=["numpy", "lancedb"])
def store_backend(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "lancedb":
        pytest.importorskip(
            "lancedb", reason="lancedb not installed (pip install 'syncsage[vector]')"
        )
        from syncsage.search.vector_store import LanceDBVectorStore

        return LanceDBVectorStore(tmp_path / "vectors")
    from syncsage.search.vector_store import NumpyVectorStore

    return NumpyVectorStore(tmp_path / "vectors")


def test_vector_store_roundtrip(store_backend: Any) -> None:
    payloads = [
        {"source_id": "s1", "artifact_id": "a1", "text_hash": "h1"},
        {"source_id": "s1", "artifact_id": "a1", "text_hash": "h2"},
        {"source_id": "s2", "artifact_id": "a2", "text_hash": "h3"},
    ]
    store_backend.upsert(
        ["c1", "c2", "c3"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
        payloads,
    )
    assert store_backend.count() == 3
    assert store_backend.existing_ids(["c1", "missing"]) == {"c1"}
    assert store_backend.source_chunk_ids("s1") == {"c1", "c2"}

    hits = store_backend.search([1.0, 0.0, 0.0], k=2)
    assert [hit[0] for hit in hits] == ["c1", "c3"]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-3)
    assert hits[0][1] >= hits[1][1]
    assert hits[0][2]["artifact_id"] == "a1"

    # Idempotent upsert: re-upserting an id replaces rather than duplicates.
    store_backend.upsert(["c1"], [[0.5, 0.5, 0.0]], [payloads[0]])
    assert store_backend.count() == 3

    assert store_backend.delete(chunk_ids=["c2"]) == 1
    assert store_backend.count() == 2
    assert store_backend.delete(artifact_id="a2") == 1
    assert store_backend.count() == 1


def test_engine_embeds_with_lancedb_backend(tmp_path: Path) -> None:
    pytest.importorskip("lancedb", reason="lancedb not installed (pip install 'syncsage[vector]')")
    engine = make_vector_engine(tmp_path, vector_provider="lancedb")
    run_sync(engine, source_name="notes", mode="full")
    assert engine.vectors.store.count() >= 2
    payload = _search(engine, "car", "vector")
    assert "vehicles.md" in _result_paths(payload)[0]


def test_numpy_store_caches_the_decoded_matrix(tmp_path: Path) -> None:
    """Regression test: search() must not re-decode the whole index every call.

    Every vector is stored base64-encoded, and decoding all of them is a
    pure-Python loop (struct.unpack per item) that does not release the GIL
    between iterations -- so redoing it on every search() call meant N
    concurrent searches cost roughly N times one search's worth of decode
    work, GIL-serialized, regardless of thread count. Measured live: an
    agentic retrieve step fanning out 4 concurrent vector searches took
    21-29s against a 7,463-chunk index. The fix caches the decoded
    (ids, matrix, norms) keyed off the same file signature `_items()`
    already uses, so it is correctness-equivalent to the uncached version
    and only rebuilds when the underlying file actually changes.
    """
    from syncsage.search.vector_store import NumpyVectorStore

    store = NumpyVectorStore(tmp_path / "vectors")
    store.upsert(
        ["c1", "c2", "c3"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
        [
            {"source_id": "s1", "artifact_id": "a1", "text_hash": "h1"},
            {"source_id": "s1", "artifact_id": "a1", "text_hash": "h2"},
            {"source_id": "s2", "artifact_id": "a2", "text_hash": "h3"},
        ],
    )

    first = store.search([1.0, 0.0, 0.0], k=2)
    cache_after_first = store._matrix_cache

    # Repeated searches against unchanged data must be identical...
    second = store.search([1.0, 0.0, 0.0], k=2)
    assert first == second
    # ...and must not have rebuilt the decoded matrix (same object, not an
    # equal-but-freshly-built one) -- this is the actual property being
    # fixed, not just "results are still correct".
    assert store._matrix_cache is cache_after_first

    # A write must invalidate the cache. Invalidation is lazy -- the matrix
    # is rebuilt on the *next search*, not eagerly on write -- so the write
    # alone changes nothing yet; only after a search does identity change,
    # and the new data is what it returns.
    store.upsert(["c4"], [[0.0, 0.0, 1.0]], [{"source_id": "s3", "artifact_id": "a3"}])
    after_write = store.search([0.0, 0.0, 1.0], k=1)
    assert after_write[0][0] == "c4"
    assert store._matrix_cache is not cache_after_first

    # Deleting must invalidate it too, not just adding.
    cache_before_delete = store._matrix_cache
    store.delete(chunk_ids=["c4"])
    remaining = store.search([0.0, 0.0, 1.0], k=5)
    assert all(hit[0] != "c4" for hit in remaining)
    assert store._matrix_cache is not cache_before_delete


def test_numpy_store_concurrent_search_is_thread_safe(tmp_path: Path) -> None:
    """Many threads racing to build the cache must not corrupt or crash."""
    import threading

    from syncsage.search.vector_store import NumpyVectorStore

    store = NumpyVectorStore(tmp_path / "vectors")
    # One-hot vectors: orthogonal, so cosine similarity to the query is
    # exactly 1.0 for its own match and exactly 0.0 for every other id --
    # unlike collinear vectors (e.g. [i, 0, 0]), which all tie at cosine
    # similarity 1.0 and make "the top hit" undefined.
    n = 200
    store.upsert(
        [f"c{i}" for i in range(n)],
        [[1.0 if j == i else 0.0 for j in range(n)] for i in range(n)],
        [{"source_id": "s", "artifact_id": f"a{i}"} for i in range(n)],
    )
    query = [1.0 if j == n - 1 else 0.0 for j in range(n)]

    results: list[list] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            hits = store.search(query, k=3)
            with lock:
                results.append(hits)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent search failed: {errors[0]!r}"
    assert len(results) == 16
    # Every thread must see the same, correct top hit.
    assert all(r[0][0] == f"c{n - 1}" for r in results)
