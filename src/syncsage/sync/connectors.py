from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from syncsage.config.schema import SourceConfig
from syncsage.ingestion.pipeline import _match_any, sha256_file, utc_now, within_max_depth
from syncsage.persistence.state_store import StateStore


class ConnectorUnavailable(RuntimeError):
    """Raised when a connector is configured but cannot run in this environment."""


@dataclass(frozen=True)
class ConnectorItem:
    identity: str
    relative_path: str
    uri: str
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    mtime: str | None = None
    etag: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorPayload:
    item: ConnectorItem
    content: bytes
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    mtime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorHealth:
    ok: bool
    status: str
    item_count: int
    checked_items: int
    errors: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] | None = None


class SourceConnector(ABC):
    connector_type = "base"
    experimental = False

    def __init__(self, source: SourceConfig, state: StateStore):
        self.source = source
        self.state = state

    @abstractmethod
    def list_items(self) -> list[ConnectorItem]:
        """Return source items that may be indexed."""

    @abstractmethod
    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        """Read one item payload."""

    def get_checkpoint(self) -> dict[str, Any] | None:
        return self.state.get_source_checkpoint(self.source.name)

    def set_checkpoint(
        self,
        cursor: dict[str, Any],
        high_watermark: dict[str, Any],
        status: str = "healthy",
    ) -> None:
        self.state.set_source_checkpoint(
            self.source.name,
            self.connector_type,
            cursor,
            high_watermark,
            utc_now(),
            status,
        )

    def resolve_identity(self, item: ConnectorItem) -> str:
        return item.identity

    def validate(self) -> ConnectorHealth:
        errors: list[str] = []
        try:
            items = self.list_items()
        except Exception as exc:
            return ConnectorHealth(
                ok=False,
                status="unhealthy",
                item_count=0,
                checked_items=0,
                errors=[str(exc)],
                checkpoint=self.get_checkpoint(),
            )
        checked = 0
        for item in items[:20]:
            try:
                self.read_item(item)
                checked += 1
            except Exception as exc:
                errors.append(f"{item.relative_path}: {exc}")
        return ConnectorHealth(
            ok=not errors,
            status="healthy" if not errors else "unhealthy",
            item_count=len(items),
            checked_items=checked,
            errors=errors,
            checkpoint=self.get_checkpoint(),
        )

    def checkpoint_from_items(
        self,
        items: list[ConnectorItem],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        identities = [item.identity for item in items]
        mtimes = [item.mtime for item in items if item.mtime]
        cursor = {
            "item_count": len(items),
            "last_identity": identities[-1] if identities else None,
        }
        high_watermark = {
            "item_count": len(items),
            "max_mtime": max(mtimes) if mtimes else None,
            "listed_at": utc_now(),
        }
        return cursor, high_watermark

    def _require_experimental_enabled(self) -> None:
        if self.experimental and not self.source.connector.allow_experimental:
            raise ConnectorUnavailable(
                f"{self.connector_type} connector for source {self.source.name} is experimental. "
                "Set sources[].connector.allow_experimental=true to enable it."
            )

    def _allows_relative_path(self, relative_path: str) -> bool:
        if not within_max_depth(relative_path, self.source.max_depth):
            return False
        if _match_any(relative_path, self.source.exclude):
            return False
        return not self.source.include or _match_any(relative_path, self.source.include)


class FilesystemConnector(SourceConnector):
    connector_type = "filesystem"

    def list_items(self) -> list[ConnectorItem]:
        root = self.source.path
        if not root.exists():
            return []
        candidates = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        items: list[ConnectorItem] = []
        for path in sorted(candidates):
            relative = path.relative_to(root if root.is_dir() else root.parent).as_posix()
            if not self._allows_relative_path(relative):
                continue
            stat = path.stat()
            digest = sha256_file(path)
            items.append(
                ConnectorItem(
                    identity=f"filesystem:{self.source.name}:{relative}",
                    relative_path=relative,
                    uri=_path_uri(path),
                    mime_type=mimetypes.guess_type(path.name)[0],
                    size_bytes=stat.st_size,
                    sha256=digest,
                    mtime=_timestamp(stat.st_mtime),
                    metadata={"path": str(path)},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        path = Path(item.metadata["path"])
        content = path.read_bytes()
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=item.mime_type,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            mtime=item.mtime,
            metadata={"path": str(path)},
        )

    def validate(self) -> ConnectorHealth:
        if not self.source.path.exists():
            return ConnectorHealth(
                ok=False,
                status="unhealthy",
                item_count=0,
                checked_items=0,
                errors=[f"path does not exist: {self.source.path}"],
                checkpoint=self.get_checkpoint(),
            )
        return super().validate()


class WebCollectionConnector(SourceConnector):
    connector_type = "web_collection"
    experimental = True

    def list_items(self) -> list[ConnectorItem]:
        self._require_experimental_enabled()
        items: list[ConnectorItem] = []
        for index, url in enumerate(self.source.urls):
            relative = _relative_url_path(url, index)
            if not self._allows_relative_path(relative):
                continue
            items.append(
                ConnectorItem(
                    identity=f"web:{url}",
                    relative_path=relative,
                    uri=url,
                    mime_type=mimetypes.guess_type(urlparse(url).path)[0],
                    metadata={"url": url},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        self._require_experimental_enabled()
        response = _urlopen(
            item.uri,
            headers=self.source.connector.headers,
            timeout=self.source.connector.request_timeout_seconds,
        )
        content = response["content"]
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=response["mime_type"] or item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=response["last_modified"],
            metadata={"url": item.uri, "headers": response["headers"]},
        )

    def checkpoint_from_items(
        self,
        items: list[ConnectorItem],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        urls = [item.uri for item in items]
        return (
            {"item_count": len(items), "last_url": urls[-1] if urls else None},
            {"item_count": len(items), "urls": urls, "listed_at": utc_now()},
        )


class APIConnector(SourceConnector):
    connector_type = "api"
    experimental = True

    def list_items(self) -> list[ConnectorItem]:
        self._require_experimental_enabled()
        endpoint = self.source.connector.api_endpoint or (
            self.source.urls[0] if self.source.urls else None
        )
        if not endpoint:
            raise ConnectorUnavailable(
                f"api connector for source {self.source.name} requires "
                "connector.api_endpoint or urls[0]"
            )
        response = _urlopen(
            endpoint,
            headers=self.source.connector.headers,
            timeout=self.source.connector.request_timeout_seconds,
        )
        payload = json.loads(response["content"].decode("utf-8"))
        raw_items = payload.get(self.source.connector.api_items_field, payload)
        if not isinstance(raw_items, list):
            raise ConnectorUnavailable(
                "API item listing must be a JSON list or contain a list field"
            )
        items: list[ConnectorItem] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or raw.get("path") or raw.get("url") or index)
            item_url = raw.get("url") or raw.get("href")
            relative = str(raw.get("path") or raw.get("name") or item_id)
            relative = _ensure_text_suffix(_safe_path(relative))
            if not self._allows_relative_path(relative):
                continue
            uri = str(item_url or f"api://{self.source.name}/{item_id}")
            items.append(
                ConnectorItem(
                    identity=f"api:{self.source.name}:{item_id}",
                    relative_path=relative,
                    uri=uri,
                    mime_type=raw.get("mime_type"),
                    sha256=raw.get("sha256"),
                    mtime=raw.get("updated_at") or raw.get("mtime"),
                    metadata=raw,
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        self._require_experimental_enabled()
        content_value = item.metadata.get(self.source.connector.api_content_field)
        if content_value is not None:
            content = str(content_value).encode("utf-8")
            return ConnectorPayload(
                item=item,
                content=content,
                mime_type=item.mime_type or "text/plain",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                mtime=item.mtime,
                metadata=item.metadata,
            )
        if not item.uri.startswith(("http://", "https://")):
            raise ConnectorUnavailable(
                f"API item {item.identity} has no readable URL or content field"
            )
        response = _urlopen(
            item.uri,
            headers=self.source.connector.headers,
            timeout=self.source.connector.request_timeout_seconds,
        )
        content = response["content"]
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=response["mime_type"] or item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=response["last_modified"] or item.mtime,
            metadata=item.metadata,
        )


class S3Connector(SourceConnector):
    connector_type = "s3"
    experimental = True

    def list_items(self) -> list[ConnectorItem]:
        self._require_experimental_enabled()
        client = _boto3_client()
        bucket = self.source.connector.s3_bucket
        prefix = self.source.connector.s3_prefix or ""
        if not bucket:
            raise ConnectorUnavailable(
                f"s3 connector for source {self.source.name} requires connector.s3_bucket"
            )
        items: list[ConnectorItem] = []
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            response = client.list_objects_v2(**kwargs)
            for obj in response.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                relative = key[len(prefix):].lstrip("/") if key.startswith(prefix) else key
                if _match_any(relative, self.source.exclude):
                    continue
                if self.source.include and not _match_any(relative, self.source.include):
                    continue
                last_modified = obj.get("LastModified")
                items.append(
                    ConnectorItem(
                        identity=f"s3:{bucket}:{key}",
                        relative_path=relative,
                        uri=f"s3://{bucket}/{key}",
                        mime_type=mimetypes.guess_type(key)[0],
                        size_bytes=obj.get("Size"),
                        sha256=None,
                        mtime=last_modified.isoformat().replace("+00:00", "Z")
                        if last_modified
                        else None,
                        etag=(obj.get("ETag") or "").strip('"') or None,
                        metadata={"bucket": bucket, "key": key},
                    )
                )
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        self._require_experimental_enabled()
        client = _boto3_client()
        response = client.get_object(Bucket=item.metadata["bucket"], Key=item.metadata["key"])
        content = response["Body"].read()
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=response.get("ContentType") or item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata=item.metadata,
        )


def connector_for_source(source: SourceConfig, state: StateStore) -> SourceConnector:
    if source.type.value in {
        "repository",
        "markdown_folder",
        "obsidian_vault",
        "document_folder",
        "single_file",
    }:
        return FilesystemConnector(source, state)
    if source.type.value == "web_collection":
        return WebCollectionConnector(source, state)
    if source.type.value == "api":
        return APIConnector(source, state)
    if source.type.value == "s3":
        return S3Connector(source, state)
    raise ConnectorUnavailable(f"No connector registered for source type: {source.type.value}")


def _timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _path_uri(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except ValueError:
        return str(path)


def _urlopen(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request_headers = {"User-Agent": "SyncSage/0.1"}
    request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        content = response.read()
        info = response.info()
        return {
            "content": content,
            "mime_type": info.get_content_type() if hasattr(info, "get_content_type") else None,
            "last_modified": info.get("Last-Modified"),
            "headers": dict(info.items()),
        }


def _relative_url_path(url: str, index: int) -> str:
    parsed = urlparse(url)
    host = _safe_segment(parsed.netloc or parsed.scheme or "url")
    path = unquote(parsed.path or "").strip("/")
    if not path:
        path = f"index-{index}.html"
    relative = f"{host}/{_safe_path(path)}"
    return _ensure_text_suffix(relative)


def _safe_segment(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized.strip("._") or "item"


def _safe_path(value: str) -> str:
    return "/".join(_safe_segment(part) for part in value.replace("\\", "/").split("/") if part)


def _ensure_text_suffix(relative: str) -> str:
    if Path(relative).suffix:
        return relative
    return f"{relative}.txt"


def _boto3_client() -> Any:
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise ConnectorUnavailable("S3 connector requires boto3 to be installed") from exc
    return boto3.client("s3")
