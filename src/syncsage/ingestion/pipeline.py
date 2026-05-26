from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from syncsage.config.schema import SourceConfig
from syncsage.ingestion.chunking import TextChunk, chunk_text
from syncsage.ingestion.content_types import TEXT_EXTENSIONS, artifact_type


@dataclass(frozen=True)
class ParsedArtifact:
    id: str
    source_id: str
    path: Path
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


def _match_any(relative: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch("/" + relative, pattern)
        for pattern in patterns
    )


def discover_files(source: SourceConfig) -> list[Path]:
    root = source.path
    if not root.exists():
        return []
    candidates = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    files: list[Path] = []
    for path in candidates:
        rel = path.relative_to(root if root.is_dir() else root.parent).as_posix()
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
    chunks = chunk_text(text, source.chunking.max_chars, source.chunking.overlap_chars)
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
