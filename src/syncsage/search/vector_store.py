"""Per-region vector self-search (Synapse step 21.4).

Embeddings are optional and disabled by default; when ``search.embeddings``
is enabled, :class:`~syncsage.sync.engine.SyncEngine` embeds chunks at sync
time and ``HybridSearch`` gains ``mode="vector"`` candidates.

On-disk layout
--------------
Vectors live under ``<vector_store.path>/<kb_id>/`` (default
``<state>/vectors/<kb_id>/``). The directory is created only when
embeddings are enabled, so a default-configured region never grows a
vector store. Two backends implement :class:`VectorStore`:

- ``NumpyVectorStore`` — always available (numpy is a core dependency).
  A single flat JSON file ``index.json`` maps each chunk id to a
  base64-encoded little-endian float32 vector plus a small payload dict
  (``source_id`` / ``artifact_id`` / ``text_hash``). Writes are durable
  via tmp + fsync + atomic rename. This is the test backend.
- ``LanceDBVectorStore`` — the production default
  (``search.vector_store.provider: lancedb``); lazily imports ``lancedb``
  and raises an actionable hint to ``pip install 'syncsage[vector]'``
  when the optional extra is missing.

Idempotency bookkeeping
-----------------------
Chunk ids are content-addressed (they embed the chunk ``text_hash``), so
"has this exact text already been embedded?" is exactly vector-store
membership of the chunk id. :class:`VectorIndexer` embeds only ids that
are missing from the store and prunes ids no longer present in the
``chunks`` table at the end of each sync — re-syncing unchanged content
(incremental *or* full) therefore performs zero embedder calls.

Embedders
---------
``OpenAISpecEmbedder`` speaks the standard OpenAI embeddings HTTP shape
(``POST {base_url}/embeddings`` with ``{"model": ..., "input": [...]}``,
response ``data[i].embedding``) — the same wire format the
subjective-retrieval router's embedding provider uses, so a Synapse fleet
can pin one model for both repos. ``StubEmbedder`` is the deterministic
offline path: each lowercase token hashes (blake2b) to a fixed unit
direction and a text embeds to the normalized sum of its token
directions; a small built-in synonym table canonicalizes tokens first
(e.g. ``automobile -> car``) so tests can surface lexically-absent
matches without any model or network.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import os
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.request import Request, urlopen

import numpy as np

from syncsage.search.sqlite_store import _row_result

if TYPE_CHECKING:
    from syncsage.config.schema import EmbeddingsSettings, SyncSageConfig
    from syncsage.persistence.state_store import StateStore

logger = logging.getLogger(__name__)

# Planted synonym groups for the deterministic stub embedder. Tokens that
# canonicalize to the same key share an embedding direction, which is how
# offline tests exercise "semantic" retrieval of lexically-absent terms.
DEFAULT_STUB_SYNONYMS: dict[str, str] = {
    "automobile": "car",
    "vehicle": "car",
    "physician": "doctor",
    "clinician": "doctor",
    "k8s": "kubernetes",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Batch text -> vector provider. ``calls`` counts transport batches."""

    model: str
    calls: int
    texts_embedded: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStore(Protocol):
    """Durable chunk-id keyed vector index."""

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None: ...

    def delete(
        self,
        chunk_ids: list[str] | None = None,
        artifact_id: str | None = None,
    ) -> int: ...

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        """Return ``(chunk_id, cosine_similarity, payload)`` best-first."""
        ...

    def count(self) -> int: ...

    def existing_ids(self, chunk_ids: list[str]) -> set[str]: ...

    def source_chunk_ids(self, source_id: str) -> set[str]: ...


class StubEmbedder:
    """Deterministic, offline embedder for tests and demos.

    Each token's direction is derived per-dimension from
    ``blake2b("{token}:{i}")`` so vectors are stable across processes and
    platforms; synonyms map onto a shared canonical token before hashing.
    """

    def __init__(
        self,
        dim: int = 64,
        model: str = "stub-embed",
        synonyms: dict[str, str] | None = None,
    ):
        self.dim = max(8, int(dim or 64))
        self.model = model
        self.synonyms = dict(DEFAULT_STUB_SYNONYMS if synonyms is None else synonyms)
        self.calls = 0
        self.texts_embedded = 0
        self._directions: dict[str, np.ndarray] = {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_embedded += len(texts)
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self.dim, dtype=np.float64)
        tokens = _TOKEN_RE.findall((text or "").lower())
        for token in tokens:
            vector += self._direction(self.synonyms.get(token, token))
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return [float(value) for value in vector]

    def _direction(self, token: str) -> np.ndarray:
        cached = self._directions.get(token)
        if cached is not None:
            return cached
        values = []
        for i in range(self.dim):
            digest = hashlib.blake2b(f"{token}:{i}".encode(), digest_size=8).digest()
            values.append(int.from_bytes(digest, "big") / 2**63 - 1.0)
        direction = np.array(values, dtype=np.float64)
        direction /= float(np.linalg.norm(direction)) or 1.0
        self._directions[token] = direction
        return direction


class OpenAISpecEmbedder:
    """OpenAI-spec HTTP embedding client (stdlib urllib, no SDK).

    Wire format: ``POST {base_url}/embeddings`` with body
    ``{"model": ..., "input": [...]}`` (+ optional ``dimensions``);
    response embeddings are read from ``data[i].embedding`` ordered by
    ``data[i].index``. The API key is read from the environment variable
    named by ``api_key_env`` at call time and never persisted.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        dimensions: int | None = None,
        batch_size: int = 64,
        timeout: int = 30,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.dimensions = int(dimensions) if dimensions else None
        self.batch_size = max(1, int(batch_size or 64))
        self.timeout = timeout
        self.calls = 0
        self.texts_embedded = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model, "input": list(batch)}
        if self.dimensions:
            body["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json", "User-Agent": "SyncSage/0.1"}
        api_key = os.environ.get(self.api_key_env or "", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.calls += 1
        self.texts_embedded += len(batch)
        data = sorted(payload.get("data", []), key=lambda item: int(item.get("index", 0)))
        if len(data) != len(batch):
            raise ValueError(
                f"Embedding endpoint returned {len(data)} embeddings for {len(batch)} inputs"
            )
        return [[float(value) for value in item["embedding"]] for item in data]


class NumpyVectorStore:
    """Always-available flat-file backend (also the offline test backend)."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.path = self.directory / "index.json"
        self._cache: dict[str, dict[str, Any]] | None = None
        self._cache_sig: tuple[int, int] | None = None

    # -- persistence -------------------------------------------------

    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _items(self) -> dict[str, dict[str, Any]]:
        signature = self._signature()
        if signature is None:
            self._cache, self._cache_sig = {}, None
            return {}
        if self._cache is None or signature != self._cache_sig:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = payload.get("items", {})
            self._cache_sig = signature
        return self._cache

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"format": "syncsage-vectors-v1", "items": items}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self._cache = items
        self._cache_sig = self._signature()

    @staticmethod
    def _encode(vector: list[float]) -> str:
        return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")

    @staticmethod
    def _decode(blob: str) -> np.ndarray:
        raw = base64.b64decode(blob.encode("ascii"))
        return np.frombuffer(raw, dtype="<f4").astype(np.float64)

    # -- VectorStore protocol ----------------------------------------

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None:
        items = dict(self._items())
        for chunk_id, vector, payload in zip(chunk_ids, vectors, payloads, strict=True):
            items[chunk_id] = {"v": self._encode(vector), "payload": payload}
        self._save(items)

    def delete(
        self,
        chunk_ids: list[str] | None = None,
        artifact_id: str | None = None,
    ) -> int:
        items = dict(self._items())
        doomed = set(chunk_ids or [])
        if artifact_id is not None:
            doomed.update(
                chunk_id
                for chunk_id, item in items.items()
                if item.get("payload", {}).get("artifact_id") == artifact_id
            )
        removed = 0
        for chunk_id in doomed:
            if items.pop(chunk_id, None) is not None:
                removed += 1
        if removed:
            self._save(items)
        return removed

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        items = self._items()
        if not items or k < 1:
            return []
        query = np.asarray(query_vec, dtype=np.float64)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            return []
        ids = list(items)
        matrix = np.stack([self._decode(items[chunk_id]["v"]) for chunk_id in ids])
        if matrix.shape[1] != query.shape[0]:
            raise ValueError(
                f"Vector dimension mismatch: index has {matrix.shape[1]}, query has "
                f"{query.shape[0]}. Re-sync with mode=full after changing embedding settings."
            )
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0.0] = 1.0
        similarities = (matrix @ query) / (norms * query_norm)
        order = np.argsort(-similarities)[:k]
        return [
            (ids[i], float(similarities[i]), dict(items[ids[i]].get("payload", {}))) for i in order
        ]

    def count(self) -> int:
        return len(self._items())

    def existing_ids(self, chunk_ids: list[str]) -> set[str]:
        items = self._items()
        return {chunk_id for chunk_id in chunk_ids if chunk_id in items}

    def source_chunk_ids(self, source_id: str) -> set[str]:
        return {
            chunk_id
            for chunk_id, item in self._items().items()
            if item.get("payload", {}).get("source_id") == source_id
        }

    def all_vectors(self) -> list[tuple[str, list[float]]]:
        """Bulk (chunk_id, vector) reader used by the contract publisher."""

        return [
            (chunk_id, list(self._decode(item["v"]))) for chunk_id, item in self._items().items()
        ]


class LanceDBVectorStore:
    """LanceDB-backed store (optional ``[vector]`` extra)."""

    TABLE = "chunks"

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._db = None

    def _database(self):
        if self._db is None:
            try:
                import lancedb
            except ModuleNotFoundError as exc:  # pragma: no cover - exercised w/o extra
                raise ModuleNotFoundError(
                    "search.vector_store.provider='lancedb' requires the optional "
                    "extra: pip install 'syncsage[vector]'"
                ) from exc
            self.directory.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.directory))
        return self._db

    def _table(self):
        db = self._database()
        if self.TABLE not in db.table_names():
            return None
        return db.open_table(self.TABLE)

    @staticmethod
    def _quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _rows(self, columns: list[str]) -> list[dict[str, Any]]:
        table = self._table()
        if table is None:
            return []
        return table.to_arrow().select(columns).to_pylist()

    # -- VectorStore protocol ----------------------------------------

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None:
        if not chunk_ids:
            return
        rows = [
            {
                "chunk_id": chunk_id,
                "vector": [float(value) for value in vector],
                "source_id": str(payload.get("source_id") or ""),
                "artifact_id": str(payload.get("artifact_id") or ""),
                "payload_json": json.dumps(payload, sort_keys=True, default=str),
            }
            for chunk_id, vector, payload in zip(chunk_ids, vectors, payloads, strict=True)
        ]
        table = self._table()
        if table is None:
            self._database().create_table(self.TABLE, data=rows)
            return
        self.delete(chunk_ids=chunk_ids)
        table.add(rows)

    def delete(
        self,
        chunk_ids: list[str] | None = None,
        artifact_id: str | None = None,
    ) -> int:
        table = self._table()
        if table is None:
            return 0
        before = table.count_rows()
        ids = list(chunk_ids or [])
        for start in range(0, len(ids), 500):
            quoted = ", ".join(self._quote(chunk_id) for chunk_id in ids[start : start + 500])
            table.delete(f"chunk_id IN ({quoted})")
        if artifact_id is not None:
            table.delete(f"artifact_id = {self._quote(artifact_id)}")
        return before - table.count_rows()

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        table = self._table()
        if table is None or k < 1:
            return []
        hits = (
            table.search([float(value) for value in query_vec])
            .distance_type("cosine")
            .limit(k)
            .to_list()
        )
        return [
            (
                hit["chunk_id"],
                1.0 - float(hit.get("_distance") or 0.0),
                json.loads(hit.get("payload_json") or "{}"),
            )
            for hit in hits
        ]

    def count(self) -> int:
        table = self._table()
        return 0 if table is None else table.count_rows()

    def existing_ids(self, chunk_ids: list[str]) -> set[str]:
        stored = {row["chunk_id"] for row in self._rows(["chunk_id"])}
        return {chunk_id for chunk_id in chunk_ids if chunk_id in stored}

    def source_chunk_ids(self, source_id: str) -> set[str]:
        return {
            row["chunk_id"]
            for row in self._rows(["chunk_id", "source_id"])
            if row["source_id"] == source_id
        }

    def all_vectors(self) -> list[tuple[str, list[float]]]:
        """Bulk (chunk_id, vector) reader used by the contract publisher."""

        return [
            (row["chunk_id"], [float(value) for value in row["vector"]])
            for row in self._rows(["chunk_id", "vector"])
        ]


class VectorIndexer:
    """Embed-on-sync helper: embeds only chunk ids missing from the store."""

    def __init__(self, embedder: Embedder, store: VectorStore):
        self.embedder = embedder
        self.store = store

    def index_artifact(
        self,
        source_id: str,
        artifact_id: str,
        chunk_rows: list[dict[str, Any]],
    ) -> int:
        """Embed + upsert new/changed chunks; returns how many were embedded.

        Chunk ids are content-addressed (``sha256={text_hash}`` is part of
        the id), so store membership doubles as the text_hash bookkeeping:
        an unchanged chunk keeps its id and is skipped without any
        embedder call.
        """

        ids = [str(chunk["id"]) for chunk in chunk_rows]
        existing = self.store.existing_ids(ids)
        pending = [chunk for chunk in chunk_rows if str(chunk["id"]) not in existing]
        if not pending:
            return 0
        vectors = self.embedder.embed([str(chunk["text"]) for chunk in pending])
        self.store.upsert(
            [str(chunk["id"]) for chunk in pending],
            vectors,
            [
                {
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "text_hash": chunk.get("text_hash"),
                }
                for chunk in pending
            ],
        )
        return len(pending)

    def prune_source(self, source_id: str, live_chunk_ids: set[str]) -> int:
        """Delete vectors for chunks (or whole artifacts) no longer indexed."""

        stale = sorted(self.store.source_chunk_ids(source_id) - set(live_chunk_ids))
        if not stale:
            return 0
        return self.store.delete(chunk_ids=stale)


class VectorSearcher:
    """Query-time vector candidates in the SearchStore result shape."""

    #: Recent query embeddings, newest last. A vector query spends most of its
    #: time waiting on the embedding provider, and the same question gets asked
    #: repeatedly — re-running a search, an agent loop retrying with the same
    #: sub-query, a user refining one word. Small and per-process on purpose:
    #: this is a latency cache, not a store.
    _QUERY_CACHE_SIZE = 256

    def __init__(self, embedder: Embedder, store: VectorStore, state: StateStore):
        self.embedder = embedder
        self.store = store
        self.state = state
        self._query_cache: dict[str, list[float]] = {}

    def embed_query(self, query: str) -> list[float]:
        """Embed a query, reusing a recent identical one."""

        cached = self._query_cache.get(query)
        if cached is not None:
            return cached
        vector = self.embedder.embed([query])[0]
        if len(self._query_cache) >= self._QUERY_CACHE_SIZE:
            # Plain FIFO eviction: dicts keep insertion order, and at this size
            # the difference between FIFO and LRU is not worth the bookkeeping.
            self._query_cache.pop(next(iter(self._query_cache)), None)
        self._query_cache[query] = vector
        return vector

    def search(
        self,
        query: str,
        source_name: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        if not (query or "").strip():
            return []
        query_vec = self.embed_query(query)
        if not any(query_vec):
            return []
        fetch = max_results * 4 if source_name else max_results
        hits = self.store.search(query_vec, k=fetch)
        if source_name:
            hits = [hit for hit in hits if hit[2].get("source_id") == source_name]
        hits = hits[:max_results]
        if not hits:
            return []
        placeholders = ",".join("?" for _ in hits)
        rows = self.state.rows(
            f"""SELECT chunks.id AS chunk_id, chunks.source_id, chunks.artifact_id,
                       artifacts.relative_path AS relative_path, chunks.heading_path,
                       chunks.text, chunks.start_line, chunks.end_line,
                       artifacts.path AS absolute_path
                FROM chunks JOIN artifacts ON artifacts.id = chunks.artifact_id
                WHERE chunks.id IN ({placeholders})""",
            tuple(chunk_id for chunk_id, _, _ in hits),
        )
        by_id = {row["chunk_id"]: row for row in rows}
        results: list[dict[str, Any]] = []
        for chunk_id, similarity, _payload in hits:
            row = by_id.get(chunk_id)
            if row is None:
                continue  # vector store ahead of/behind SQLite; skip orphans
            score = max(0.0, min(1.0, (1.0 + similarity) / 2.0))
            results.append(_row_result(row, len(results) + 1, score, "Vector similarity"))
        return results


def build_embedder(settings: EmbeddingsSettings) -> Embedder:
    provider = (settings.provider or "").lower()
    if provider == "stub":
        return StubEmbedder(dim=settings.dimensions, model=settings.model)
    if provider in {"openai-spec", "openai"}:
        return OpenAISpecEmbedder(
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
            dimensions=settings.dimensions,
            batch_size=settings.batch_size,
        )
    raise ValueError(
        f"Unsupported search.embeddings.provider {settings.provider!r}; "
        "expected 'openai-spec' or 'stub'"
    )


def build_vector_store(config: SyncSageConfig) -> VectorStore:
    settings = config.search.vector_store
    base = settings.path or (config.syncsage.state_path / "vectors")
    directory = Path(base) / config.knowledge_base_id
    provider = (settings.provider or "lancedb").lower()
    if provider == "numpy":
        return NumpyVectorStore(directory)
    if provider == "lancedb":
        return LanceDBVectorStore(directory)
    raise ValueError(
        f"Unsupported search.vector_store.provider {settings.provider!r}; "
        "expected 'lancedb' or 'numpy'"
    )


#: Backends ``build_vector_store`` knows how to construct, with the label and
#: install hint the UI shows. ``numpy`` needs nothing beyond the core deps.
VECTOR_STORE_PROVIDERS: tuple[tuple[str, str, str | None], ...] = (
    ("numpy", "Flat file (numpy)", None),
    ("lancedb", "LanceDB", "pip install 'syncsage[vector]'"),
)


def vector_store_available(provider: str) -> bool:
    """Can this backend actually run in this process?

    Backends import lazily so that a region configured for LanceDB but never
    used doesn't pay the import, which also means "constructed" is not
    "usable". Callers that need to know *before* touching the store — the
    config UI offering a choice, the API refusing to claim embeddings are
    active — ask here.
    """

    name = (provider or "").lower()
    if name == "numpy":
        return True
    if name == "lancedb":
        try:
            return importlib.util.find_spec("lancedb") is not None
        except (ImportError, ValueError):  # namespace shadowing, broken install
            return False
    return False


def vector_indexer_from_config(config: SyncSageConfig) -> VectorIndexer | None:
    """Build the embed-on-sync indexer, or ``None`` when disabled.

    Unrecognized providers (e.g. pre-21.4 example configs with
    ``provider: local`` / ``engine: chroma``) log a warning and behave as
    disabled instead of breaking an existing standalone deployment. A
    missing ``lancedb`` extra, in contrast, raises with an install hint.
    """

    settings = config.search.embeddings
    if not settings.enabled:
        return None
    try:
        return VectorIndexer(build_embedder(settings), build_vector_store(config))
    except ValueError as exc:
        logger.warning("Vector search disabled: %s", exc)
        return None


def vector_searcher_from_config(
    config: SyncSageConfig,
    state: StateStore,
) -> VectorSearcher | None:
    indexer = vector_indexer_from_config(config)
    if indexer is None:
        return None
    return VectorSearcher(indexer.embedder, indexer.store, state)
