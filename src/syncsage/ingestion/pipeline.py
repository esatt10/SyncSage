from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from syncsage.config.schema import SourceConfig
from syncsage.ingestion.chunking import TextChunk, chunk_text
from syncsage.ingestion.content_types import TEXT_EXTENSIONS, artifact_type

if TYPE_CHECKING:
    from syncsage.sync.connectors import ConnectorItem, ConnectorPayload


@dataclass(frozen=True)
class ParsedArtifact:
    id: str
    source_id: str
    path: Path | str
    relative_path: str
    type: str
    mime_type: str | None
    size_bytes: int
    sha256: str
    mtime: str
    git_branch: str | None
    git_commit: str | None
    chunks: list[TextChunk]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _match_any(relative: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch("/" + relative, pattern)
        for pattern in patterns
    )


def within_max_depth(relative: str, max_depth: int | None) -> bool:
    if max_depth is None:
        return True
    depth = max(0, len(Path(relative).parts) - 1)
    return depth <= max(0, max_depth)


def discover_files(source: SourceConfig) -> list[Path]:
    root = source.path
    if not root.exists():
        return []
    candidates = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    files: list[Path] = []
    for path in candidates:
        rel = path.relative_to(root if root.is_dir() else root.parent).as_posix()
        if not within_max_depth(rel, source.max_depth):
            continue
        if _match_any(rel, source.exclude):
            continue
        if source.include and not _match_any(rel, source.include):
            continue
        files.append(path)
    return sorted(files)


def git_state(root: Path) -> tuple[str | None, str | None, bool]:
    cwd = root if root.is_dir() else root.parent
    try:
        branch = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return branch, commit, False
    except Exception:
        return None, None, False


def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".pdf", ".docx"}:
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def read_text_bytes(content: bytes, relative_path: str) -> str:
    suffix = Path(relative_path).suffix.lower()
    if suffix in {".pdf", ".docx"}:
        return ""
    return content.decode("utf-8", errors="ignore")


def chunks_for_source(source: SourceConfig, text: str) -> list[TextChunk]:
    if not text:
        return []
    if not source.chunking.enabled:
        lines = text.splitlines()
        return [
            TextChunk(
                index=0,
                text=text,
                start_line=1,
                end_line=len(lines) or 1,
            )
        ]
    return chunk_text(text, source.chunking.max_chars, source.chunking.overlap_chars)


def parse_file(
    source: SourceConfig,
    path: Path,
    git_metadata: tuple[str | None, str | None, bool] | None = None,
) -> ParsedArtifact | None:
    if path.suffix.lower() not in TEXT_EXTENSIONS | {".pdf", ".docx"}:
        return None
    root = source.path if source.path.is_dir() else source.path.parent
    relative = path.relative_to(root).as_posix()
    digest = sha256_file(path)
    branch, commit, _dirty = (
        git_metadata
        if git_metadata is not None
        else git_state(root)
        if source.type.value == "repository"
        else (None, None, False)
    )
    text = read_text(path)
    chunks = chunks_for_source(source, text)
    stat = path.stat()
    artifact_id = f"file:{source.name}:{relative}:branch={branch or 'none'}"
    return ParsedArtifact(
        id=artifact_id,
        source_id=source.name,
        path=path,
        relative_path=relative,
        type=artifact_type(path),
        mime_type=None,
        size_bytes=stat.st_size,
        sha256=digest,
        mtime=datetime.fromtimestamp(stat.st_mtime, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        git_branch=branch,
        git_commit=commit,
        chunks=chunks,
    )


def parse_connector_payload(
    source: SourceConfig,
    item: ConnectorItem,
    payload: ConnectorPayload,
    git_metadata: tuple[str | None, str | None, bool] | None = None,
) -> ParsedArtifact | None:
    suffix = Path(item.relative_path).suffix.lower()
    mime_type = payload.mime_type or item.mime_type
    if suffix not in TEXT_EXTENSIONS | {".pdf", ".docx"} and not _is_text_like(mime_type):
        return None
    branch, commit, _dirty = (
        git_metadata
        if git_metadata is not None
        else git_state(source.path)
        if source.type.value == "repository"
        else (None, None, False)
    )
    content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
    text = read_text_bytes(payload.content, item.relative_path)
    chunks = chunks_for_source(source, text)
    artifact_id = f"file:{source.name}:{item.relative_path}:branch={branch or 'none'}"
    return ParsedArtifact(
        id=artifact_id,
        source_id=source.name,
        path=payload.metadata.get("path") or item.uri,
        relative_path=item.relative_path,
        type=artifact_type(Path(item.relative_path)),
        mime_type=mime_type,
        size_bytes=payload.size_bytes or item.size_bytes or len(payload.content),
        sha256=content_hash,
        mtime=payload.mtime or item.mtime or utc_now(),
        git_branch=branch,
        git_commit=commit,
        chunks=chunks,
    )


def _is_text_like(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    return (
        mime_type.startswith("text/")
        or mime_type in {"application/json", "application/xml", "application/x-yaml"}
    )
