"""Authenticated stateless coordinator/worker protocol for text preparation."""

from __future__ import annotations

import base64
import json
import os
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


def parsed_from_wire(payload: dict[str, Any] | None) -> ParsedArtifact | None:
    if payload is None:
        return None
    return ParsedArtifact(
        id=str(payload["id"]),
        source_id=str(payload["source_id"]),
        path=str(payload["path"]),
        relative_path=str(payload["relative_path"]),
        type=str(payload["type"]),
        mime_type=payload.get("mime_type"),
        size_bytes=int(payload["size_bytes"]),
        sha256=str(payload["sha256"]),
        mtime=str(payload["mtime"]),
        git_branch=payload.get("git_branch"),
        git_commit=payload.get("git_commit"),
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
    return parsed_from_wire(body.get("parsed"))
