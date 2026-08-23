"""Authenticated stateless coordinator/worker protocol for text preparation."""

from __future__ import annotations

import base64
import json
import os
import threading
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from pheasant.config.schema import PheasantConfig, SourceConfig
from pheasant.ingestion.chunking import TextChunk
from pheasant.ingestion.content_types import TEXT_EXTENSIONS
from pheasant.ingestion.pipeline import ParsedArtifact, parse_connector_payload
from pheasant.sync.connectors import ConnectorItem, ConnectorPayload


class RemoteWorkerError(RuntimeError):
    """A remote preparation worker could not complete an immutable task."""


REMOTE_TEXT_EXTENSIONS = TEXT_EXTENSIONS - {".html"}


def configured_token(env_name: str) -> str:
    token = os.environ.get(env_name or "", "")
    if not token:
        raise RemoteWorkerError(
            f"Remote indexing requires a token in environment variable {env_name!r}"
        )
    return token


def parsed_to_wire(parsed: ParsedArtifact | None) -> dict[str, Any] | None:
    if parsed is None:
        return None
    if parsed.headings:
        raise RemoteWorkerError("Remote text workers do not accept taxonomy-bearing results")
    return {
        "id": parsed.id,
        "source_id": parsed.source_id,
        "path": str(parsed.path),
        "relative_path": parsed.relative_path,
        "type": parsed.type,
        "mime_type": parsed.mime_type,
        "size_bytes": parsed.size_bytes,
        "sha256": parsed.sha256,
        "mtime": parsed.mtime,
        "git_branch": parsed.git_branch,
        "git_commit": parsed.git_commit,
        "chunks": [asdict(chunk) for chunk in parsed.chunks],
        "headings": [],
    }


def parsed_from_wire(payload: dict[str, Any] | None, task: dict[str, Any]) -> ParsedArtifact | None:
    """Build a ``ParsedArtifact`` from a worker's response — but never trust
    the worker for ``id``/``source_id``/``path``/``relative_path``/
    ``git_branch``/``git_commit``.

    Security audit finding H5: those six fields used to come straight from
    ``payload``, and only ``source_id``/``relative_path``/``sha256`` were
    ever checked against the coordinator's own expectation (in
    ``engine.py``, after this function returns). ``id`` and ``path`` were
    committed to the ``artifacts`` table verbatim — ``id`` collides with
    (and silently overwrites) another artifact's row and graph node if a
    worker names one that already exists; ``path`` is later opened
    straight off disk by the unauthenticated ``GET /nodes/content``. A
    compromised worker, or anyone who recovers the shared bearer token off
    the cleartext transport (see ``sync/grpc_worker.py``'s TLS note), could
    use either as a primitive.

    The fix is not to validate the wire values but to never read them:
    ``task`` is the coordinator's own record of what it asked this worker
    to prepare (``task_payload``'s output, still in hand at both call
    sites — ``prepare_remote`` and the gRPC/HTTP batch transports), and
    every one of these six fields is a pure, deterministic function of
    ``task["source"]``/``task["item"]``/``task["git_metadata"]`` — the
    exact grammar ``ingestion/pipeline.py::parse_connector_payload`` uses
    locally. A legitimate worker's answer was always going to compute the
    identical values from those identical inputs, so recomputing here is
    lossless for a correct worker and closes the primitive for a
    dishonest one.
    """
    if payload is None:
        return None
    source = task["source"]
    item = task["item"]
    git_metadata = task.get("git_metadata")
    branch = git_metadata[0] if git_metadata else None
    commit = git_metadata[1] if git_metadata else None
    relative_path = str(item["relative_path"])
    source_name = str(source["name"])
    path = (task.get("payload") or {}).get("metadata", {}).get("path") or item.get("uri")
    return ParsedArtifact(
        id=f"file:{source_name}:{relative_path}:branch={branch or 'none'}",
        source_id=source_name,
        path=str(path),
        relative_path=relative_path,
        type=str(payload["type"]),
        mime_type=payload.get("mime_type"),
        size_bytes=int(payload["size_bytes"]),
        sha256=str(payload["sha256"]),
        mtime=str(payload["mtime"]),
        git_branch=branch,
        git_commit=commit,
        chunks=[TextChunk(**row) for row in payload.get("chunks") or []],
        headings=[],
    )


def task_payload(
    source: SourceConfig,
    item: ConnectorItem,
    payload: ConnectorPayload,
    git_metadata: tuple[str | None, str | None, bool] | None,
) -> dict[str, Any]:
    return {
        # Only parsing inputs cross the trust boundary. In particular, do not
        # forward connector headers: they may contain source credentials and a
        # stateless text worker has no reason to receive them.
        "source": {
            "name": source.name,
            "type": source.type.value,
            "path": str(source.path),
            "chunking": source.chunking.model_dump(mode="json"),
            "taxonomy": source.taxonomy.model_dump(mode="json"),
        },
        "item": asdict(item),
        "payload": {
            "content_base64": base64.b64encode(payload.content).decode("ascii"),
            "mime_type": payload.mime_type,
            "size_bytes": payload.size_bytes,
            "sha256": payload.sha256,
            "mtime": payload.mtime,
            "metadata": payload.metadata,
        },
        "git_metadata": list(git_metadata) if git_metadata is not None else None,
    }


def prepare_task(task: dict[str, Any]) -> dict[str, Any] | None:
    """Execute one immutable preparation task without touching state or graph."""

    source = PheasantConfig.model_validate({"sources": [task["source"]]}).sources[0]
    item = ConnectorItem(**task["item"])
    if source.taxonomy.enabled:
        raise RemoteWorkerError("Remote preparation does not support taxonomy-enabled sources")
    if Path(item.relative_path).suffix.lower() not in REMOTE_TEXT_EXTENSIONS:
        raise RemoteWorkerError(
            f"Remote preparation only accepts ordinary text: {item.relative_path}"
        )
    raw = task["payload"]
    payload = ConnectorPayload(
        item=item,
        content=base64.b64decode(raw["content_base64"], validate=True),
        mime_type=raw.get("mime_type"),
        size_bytes=raw.get("size_bytes"),
        sha256=raw.get("sha256"),
        mtime=raw.get("mtime"),
        metadata=raw.get("metadata") or {},
    )
    git_raw = task.get("git_metadata")
    git_metadata = tuple(git_raw) if git_raw is not None else None
    parsed = parse_connector_payload(source, item, payload, git_metadata)  # type: ignore[arg-type]
    return parsed_to_wire(parsed)


class ResultCache:
    """Bounded content-addressed cache of prepared results (Phase 35.5).

    Preparation is a pure function of the task, so a retry after a timeout —
    the case at-least-once delivery exists to make safe — asks for something
    the worker may already have computed. Answering it from here turns the
    duplicate into a lookup instead of a second parse.

    Bounded and LRU on purpose: a worker serving a 50k-file source must not
    accumulate 50k parse results, and the value of an entry decays fast (a
    retry follows its original within one deadline, not one sync).
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = max(1, int(maxsize))
        self._entries: OrderedDict[str, dict[str, Any] | None] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[bool, dict[str, Any] | None]:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self.hits += 1
                return True, self._entries[key]
            self.misses += 1
            return False, None

    def put(self, key: str, value: dict[str, Any] | None) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class DeadlineExceeded(RemoteWorkerError):
    """The caller's budget ran out before this task was started.

    Raised rather than answered: a worker that returns an answer after its
    caller gave up has spent CPU that the next, still-live task needed. The
    coordinator has already moved on or retried elsewhere.
    """


def prepare_batch_tasks(
    tasks: list[dict[str, Any]],
    keys: list[str] | None = None,
    *,
    cache: ResultCache | None = None,
    deadline: Any = None,
) -> list[dict[str, Any]]:
    """Prepare several tasks, returning one ``{"parsed": ...}`` entry each.

    ``deadline`` is a zero-argument callable returning the seconds remaining
    (or ``None`` for no budget). It is checked *between* tasks: a batch whose
    caller has given up stops rather than finishing work nobody is waiting for.

    **The completed tasks are not returned** — an earlier version of this
    docstring said they were, and they never could be: the coordinator
    requires one result per task (a short list is rejected as a malformed
    answer), so a partial batch has nowhere to go on the wire. What survives
    is the *work*, in ``cache``: each task is stored under its idempotency key
    as it finishes, so the retry that follows the 408 is a lookup rather than
    a second parse. That only holds when the caller supplies keys, which the
    coordinator always does; without them a deadline abort really does discard
    the finished work, and the loop below is where that would change.
    """

    supplied = list(keys or [])
    results: list[dict[str, Any]] = []
    for position, task in enumerate(tasks):
        if deadline is not None and position:
            remaining = deadline()
            if remaining is not None and remaining <= 0:
                raise DeadlineExceeded(
                    f"caller deadline passed after {position} of {len(tasks)} tasks"
                )
        key = supplied[position] if position < len(supplied) else ""
        if cache is not None and key:
            found, cached = cache.get(key)
            if found:
                results.append({"parsed": cached})
                continue
        parsed = prepare_task(task)
        if cache is not None and key:
            cache.put(key, parsed)
        results.append({"parsed": parsed})
    return results


def prepare_remote(
    endpoint: str,
    token: str,
    task: dict[str, Any],
    *,
    timeout: float,
) -> ParsedArtifact | None:
    url = endpoint.rstrip("/") + "/internal/indexing/prepare"
    request = Request(
        url,
        data=json.dumps(task, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "pheasant-index-coordinator/1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RemoteWorkerError(f"Remote preparation failed at {url}: {exc}") from exc
    return parsed_from_wire(body.get("parsed"), task)
