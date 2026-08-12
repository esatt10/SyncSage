"""Per-region vector self-search (Synapse step 21.4).

Embeddings are optional and disabled by default; when ``search.embeddings``
is enabled, :class:`~pheasant.sync.engine.SyncEngine` embeds chunks at sync
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
  and raises an actionable hint to ``pip install 'pheasant-kb[vector]'``
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
pheasant-flock router's embedding provider uses, so a Synapse fleet
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
import socket
import ssl
import struct
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

from pheasant.search.sqlite_store import _row_result

if TYPE_CHECKING:
    from pheasant.config.schema import EmbeddingsSettings, PheasantConfig
    from pheasant.persistence.state_store import StateStore

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

    def reset(self) -> int:
        """Remove every vector and any backend schema tied to its dimensions."""
        ...

    def flush(self) -> None:
        """Persist any writes a backend may have deferred. Backends that
        already write durably on every `upsert`/`delete` (e.g. LanceDB) make
        this a no-op; callers must still call it at the end of a sync to
        guarantee durability for backends that don't (e.g. `NumpyVectorStore`,
        see its docstring)."""
        ...


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


#: HTTP statuses worth trying again: overload, rate limiting, and the gateway
#: family. Explicitly *not* 400/401/403/404 — a malformed request or a wrong
#: key fails identically on every retry, so retrying only spends time.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Transport-level failures. `ssl.SSLError` is first because it is the one a
#: real 12k-file index actually died on.
_TRANSIENT_ERRORS = (ssl.SSLError, URLError, TimeoutError, ConnectionError, socket.timeout)

#: Ceiling on the backoff, so a long outage retries steadily rather than
#: sleeping for minutes on the last attempt.
_MAX_BACKOFF_SECONDS = 30.0


def _retry_after_seconds(header: str | None, fallback: float) -> float:
    """Parse a `Retry-After` value, falling back to our own backoff."""
    if not header:
        return fallback
    try:
        return max(0.0, min(float(header), _MAX_BACKOFF_SECONDS))
    except (TypeError, ValueError):
        # The HTTP-date form is legal but rare from these APIs; our own
        # backoff is a better answer than parsing it wrong.
        return fallback


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
        max_retries: int = 4,
        retry_backoff_seconds: float = 1.0,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.dimensions = int(dimensions) if dimensions else None
        self.batch_size = max(1, int(batch_size or 64))
        self.timeout = timeout
        # Bounded retry on transient transport failures — see _post_with_retry.
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.1, float(retry_backoff_seconds))
        self.calls = 0
        self.texts_embedded = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def _post_with_retry(self, request: Request) -> dict[str, Any]:
        """POST with bounded exponential backoff on *transient* failures.

        Indexing a large corpus is hundreds of HTTPS calls — 12,667 files of
        microsoft/vscode took over 200 — and without this a single flaky one
        aborts the whole sync. That is not hypothetical: a real run died ~45
        minutes in on

            ssl.SSLError: [SSL: SSLV3_ALERT_BAD_RECORD_MAC]

        which is a corrupted TLS record, i.e. precisely the sort of thing that
        succeeds on the next attempt. Resuming is cheap (the sha256 pre-read
        skip means unchanged files are not re-embedded), but a multi-hour index
        should not need a human to notice and restart it.

        Only *transient* conditions are retried. A 401 is a wrong key and a 400
        is a malformed request; retrying either burns time and money to fail
        the same way, so both surface immediately.
        """
        delay = self.retry_backoff_seconds
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:  # noqa: PERF203 - retry loop
                if exc.code not in _RETRYABLE_STATUS or attempt == self.max_retries:
                    raise
                last = exc
                # Honour the server's own pacing when it offers one; guessing
                # shorter than a stated Retry-After is how a 429 becomes a ban.
                header = exc.headers.get("Retry-After") if exc.headers else None
                wait = _retry_after_seconds(header, delay)
            except _TRANSIENT_ERRORS as exc:
                if attempt == self.max_retries:
                    raise
                last = exc
                wait = delay
            logger.warning(
                "embedding request failed (%s), retrying in %.1fs [%d/%d]",
                type(last).__name__,
                wait,
                attempt + 1,
                self.max_retries,
            )
            time.sleep(wait)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
        raise RuntimeError("unreachable: retry loop exhausted without raising")

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model, "input": list(batch)}
        if self.dimensions:
            body["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json", "User-Agent": "pheasant/0.1"}
        api_key = os.environ.get(self.api_key_env or "", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._post_with_retry(request)
        self.calls += 1
        self.texts_embedded += len(batch)
        data = sorted(payload.get("data", []), key=lambda item: int(item.get("index", 0)))
        if len(data) != len(batch):
            raise ValueError(
                f"Embedding endpoint returned {len(data)} embeddings for {len(batch)} inputs"
            )
        return [[float(value) for value in item["embedding"]] for item in data]


class NumpyVectorStore:
    """Always-available flat-file backend (also the offline test backend).

    ``index.json`` holds every vector in the knowledge base as one JSON
    object, so a write is always "read the whole thing, merge, write the
    whole thing back" — there is no incremental/append path, unlike a real
    embedded database (`LanceDBVectorStore`). Writing on *every*
    `upsert()` call (one per artifact, i.e. once per file — see
    `VectorIndexer.index_artifact`) is therefore O(n^2) in the number of
    artifacts once the index is non-trivially large: each of N files pays a
    full rewrite of an index that has grown to include the previous N-1.
    Measured live indexing a second large source into an already-large
    shared index (both sources share one index per knowledge base): over
    100GB written for a ~150MB final file, entirely from this pattern.
    `upsert`/`delete` now buffer in memory and flush to disk on the same
    self-throttled schedule `SyncEngine._maybe_checkpoint` already uses for
    the graph — `flush()` forces a save and callers (`SyncEngine`) MUST call
    it at the end of a sync to guarantee durability for whatever hasn't hit
    the periodic threshold yet.
    """

    def __init__(self, directory: str | Path, *, flush_interval_seconds: float = 20.0):
        self.directory = Path(directory)
        self.path = self.directory / "index.json"
        self._cache: dict[str, dict[str, Any]] | None = None
        self._cache_sig: tuple[int, int] | None = None
        # Bumped on every *content* change to `_cache` — an upsert/delete
        # (flushed or not) or a disk re-read that found different content.
        # `_cache_sig` (the on-disk file's mtime+size) does NOT change while
        # a write is buffered in memory, so it cannot be used to invalidate
        # the decoded-matrix cache below; this can.
        self._version = 0
        self._dirty = False
        self._flush_interval = max(1.0, float(flush_interval_seconds))
        self._last_flush = time.monotonic()
        self._last_flush_seconds = 0.0
        # Decoded (ids, matrix, norms) built from `_cache`. Every vector is
        # stored base64-encoded, and decoding it is a pure-Python loop
        # (base64.b64decode + struct.unpack per item) that does not release
        # the GIL between iterations -- so without caching the *decoded*
        # form, every concurrent search() call redid the full decode from
        # scratch, and the GIL serialized that CPU-bound work across
        # threads regardless of how many search() calls ran "concurrently".
        # Measured: an agentic retrieve step fanning out 4 concurrent
        # searches against a 7,463-chunk index took 21-29s (~5-7s each) --
        # almost entirely this decode, not the embedding API call or the
        # actual similarity math (a real numpy matmul over an already-
        # decoded matrix is fast). Keyed off `_version`, so it invalidates
        # on any content change, flushed to disk or still buffered.
        self._matrix_cache: tuple[list[str], Any, Any] | None = None
        self._matrix_sig: int | None = None
        self._matrix_lock = threading.Lock()

    # -- persistence -------------------------------------------------

    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _items(self) -> dict[str, dict[str, Any]]:
        if self._dirty:
            # Buffered writes are ahead of disk (or disk has nothing yet) —
            # re-reading here would silently discard them.
            return self._cache if self._cache is not None else {}
        signature = self._signature()
        if signature is None:
            self._cache, self._cache_sig = {}, None
            return {}
        if self._cache is None or signature != self._cache_sig:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = payload.get("items", {})
            self._cache_sig = signature
            self._version += 1
        return self._cache

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"format": "pheasant-vectors-v1", "items": items}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self._cache = items
        self._cache_sig = self._signature()

    def _maybe_flush(self, *, force: bool = False) -> None:
        if not self._dirty:
            return
        # Self-throttling, same shape as SyncEngine._maybe_checkpoint: a
        # save's cost scales with index size (the whole thing is rewritten
        # every time), so spacing flushes at ~10x the last save's duration
        # keeps their overhead a bounded fraction of the sync no matter how
        # large the index gets, instead of a full rewrite per artifact.
        interval = max(self._flush_interval, self._last_flush_seconds * 10)
        if not force and time.monotonic() - self._last_flush < interval:
            return
        started = time.monotonic()
        self._save(self._cache or {})
        self._last_flush_seconds = time.monotonic() - started
        self._last_flush = time.monotonic()
        self._dirty = False

    def flush(self) -> None:
        self._maybe_flush(force=True)

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
        self._cache = items
        self._version += 1
        self._dirty = True
        self._maybe_flush()

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
            self._cache = items
            self._version += 1
            self._dirty = True
            self._maybe_flush()
        return removed

    def _decoded_matrix(self) -> tuple[list[str], Any, Any] | None:
        """The whole index as ``(ids, matrix, norms)``, decoded once per file version.

        `_items()` already avoids re-reading the file when it hasn't changed;
        this avoids redoing the (much more expensive) per-vector decode and
        the full-matrix norm computation on every call. The check-then-build
        is locked so N threads racing in on a cold/stale cache build it once,
        not N times; once built, `self._matrix_cache` is only ever replaced by
        a fresh atomic assignment (never mutated in place), so reads of an
        already-built cache need no lock.
        """

        items = self._items()
        if not items:
            return None
        cached = self._matrix_cache
        if cached is not None and self._matrix_sig == self._version:
            return cached
        with self._matrix_lock:
            # Re-check: another thread may have just finished building it
            # while this one was waiting for the lock.
            if self._matrix_cache is not None and self._matrix_sig == self._version:
                return self._matrix_cache
            ids = list(items)
            matrix = np.stack([self._decode(items[chunk_id]["v"]) for chunk_id in ids])
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms == 0.0] = 1.0
            built = (ids, matrix, norms)
            self._matrix_cache = built
            self._matrix_sig = self._version
            return built

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        decoded = self._decoded_matrix()
        if decoded is None or k < 1:
            return []
        ids, matrix, norms = decoded
        query = np.asarray(query_vec, dtype=np.float64)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            return []
        if matrix.shape[1] != query.shape[0]:
            raise ValueError(
                f"Vector dimension mismatch: index has {matrix.shape[1]}, query has "
                f"{query.shape[0]}. Re-sync with mode=full after changing embedding settings."
            )
        similarities = (matrix @ query) / (norms * query_norm)
        order = np.argsort(-similarities)[:k]
        # A second `_items()` call: writes to this store only ever happen
        # from the separate sync-worker process (never from this one), so an
        # id vanishing between the two calls in this process is not possible
        # in practice -- `.get()` here is defensive-in-depth, not a race this
        # process can trigger on its own.
        items = self._items()
        return [
            (ids[i], float(similarities[i]), dict(items.get(ids[i], {}).get("payload", {})))
            for i in order
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

    def reset(self) -> int:
        """Clear the regenerable index and its decoded-matrix cache."""

        removed = len(self._items())
        self._cache = {}
        self._version += 1
        self._dirty = True
        self._matrix_cache = None
        self._matrix_sig = None
        self._maybe_flush(force=True)
        return removed

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
                    "extra: pip install 'pheasant-kb[vector]'"
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

    def reset(self) -> int:
        """Drop the table so the next insert can establish a new vector width.

        Deleting every row is insufficient for LanceDB: an empty table keeps
        its Arrow ``FixedSizeList`` schema, so switching from a 1,536- to a
        3,072-dimensional model still fails on the first new insert.
        """

        table = self._table()
        if table is None:
            return 0
        removed = table.count_rows()
        self._database().drop_table(self.TABLE)
        return removed

    def flush(self) -> None:
        """No-op: `upsert`/`delete` already write through to the LanceDB
        table on every call — nothing is ever buffered here."""

    def all_vectors(self) -> list[tuple[str, list[float]]]:
        """Bulk (chunk_id, vector) reader used by the contract publisher."""

        return [
            (row["chunk_id"], [float(value) for value in row["vector"]])
            for row in self._rows(["chunk_id", "vector"])
        ]


class VectorIndexer:
    """Embed-on-sync helper: embeds only chunk ids missing from the store.

    New/changed chunks are queued across files rather than embedded one file
    at a time. The sync loop calls `index_artifact` once per file, and most
    files in a real corpus carry far fewer chunks than an embedder's own
    `batch_size` (64) — embedding immediately, per file, turned a sync into
    one HTTP round-trip to the embedding provider *per file* instead of
    packing many files' chunks into one request. On a few-hundred-file repo
    that was the difference between a handful of embedding calls and
    hundreds, entirely serial on the sync's only thread. Queued chunks are
    flushed automatically once `queue_size` accumulates (so a large source
    still embeds in bounded batches, and a crash mid-sync loses at most one
    batch's progress) and explicitly by `flush()`, which the caller
    (`SyncEngine`) already calls at the end of every sync.
    """

    def __init__(self, embedder: Embedder, store: VectorStore, queue_size: int | None = None):
        self.embedder = embedder
        self.store = store
        # Default to the embedder's own batch size: `embed()` already
        # sub-batches internally at that size, so queuing exactly that many
        # pending chunks before flushing yields one HTTP call per flush
        # rather than a request smaller than what the provider was
        # configured to accept in one round-trip.
        self.queue_size = int(queue_size or getattr(embedder, "batch_size", 64) or 64)
        self._pending: list[dict[str, Any]] = []

    def index_artifact(
        self,
        source_id: str,
        artifact_id: str,
        chunk_rows: list[dict[str, Any]],
    ) -> int:
        """Queue new/changed chunks for embedding; returns how many were queued.

        Chunk ids are content-addressed (``sha256={text_hash}`` is part of
        the id), so store membership doubles as the text_hash bookkeeping:
        an unchanged chunk keeps its id and is skipped without ever
        reaching the embedder. Queued chunks are not yet in the store —
        callers that need durability before returning (as opposed to by the
        end of the sync) should call `flush()`.
        """

        ids = [str(chunk["id"]) for chunk in chunk_rows]
        existing = self.store.existing_ids(ids)
        pending = [chunk for chunk in chunk_rows if str(chunk["id"]) not in existing]
        if not pending:
            return 0
        for chunk in pending:
            self._pending.append(
                {
                    "id": str(chunk["id"]),
                    "text": str(chunk["text"]),
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "text_hash": chunk.get("text_hash"),
                }
            )
        if len(self._pending) >= self.queue_size:
            self.flush_pending()
        return len(pending)

    def flush_pending(self) -> None:
        """Embed and upsert everything queued so far, across every file
        `index_artifact` has touched since the last flush."""
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        vectors = self.embedder.embed([item["text"] for item in batch])
        self.store.upsert(
            [item["id"] for item in batch],
            vectors,
            [
                {
                    "source_id": item["source_id"],
                    "artifact_id": item["artifact_id"],
                    "text_hash": item["text_hash"],
                }
                for item in batch
            ],
        )

    def prune_source(self, source_id: str, live_chunk_ids: set[str]) -> int:
        """Delete vectors for chunks (or whole artifacts) no longer indexed.

        Only considers chunks already *in the store* — a chunk still sitting
        in the pending queue (not yet embedded) is never mistaken for stale,
        since it cannot be a member of `store.source_chunk_ids` yet.
        """

        stale = sorted(self.store.source_chunk_ids(source_id) - set(live_chunk_ids))
        if not stale:
            return 0
        return self.store.delete(chunk_ids=stale)

    def reset(self) -> int:
        """Discard pending work and reset the store's vector-space schema."""

        self._pending = []
        return self.store.reset()

    def flush(self) -> None:
        """Embed anything still queued, then force the store's writes to
        disk now. The caller (`SyncEngine`) MUST call this at the end of a
        sync, alongside its own final graph save — see `NumpyVectorStore`'s
        docstring for why the disk-flush half exists."""
        self.flush_pending()
        self.store.flush()


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
            max_retries=getattr(settings, "max_retries", 4),
            retry_backoff_seconds=getattr(settings, "retry_backoff_seconds", 1.0),
        )
    raise ValueError(
        f"Unsupported search.embeddings.provider {settings.provider!r}; "
        "expected 'openai-spec' or 'stub'"
    )


def build_vector_store(config: PheasantConfig) -> VectorStore:
    settings = config.search.vector_store
    base = settings.path or (config.pheasant.state_path / "vectors")
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
    ("lancedb", "LanceDB", "pip install 'pheasant-kb[vector]'"),
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


def vector_indexer_from_config(config: PheasantConfig) -> VectorIndexer | None:
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
    config: PheasantConfig,
    state: StateStore,
) -> VectorSearcher | None:
    indexer = vector_indexer_from_config(config)
    if indexer is None:
        return None
    return VectorSearcher(indexer.embedder, indexer.store, state)
