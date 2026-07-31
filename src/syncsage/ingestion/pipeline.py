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
from syncsage.ingestion.content_types import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
    artifact_type,
)

if TYPE_CHECKING:
    from syncsage.ingestion.captioner import Captioner
    from syncsage.ingestion.transcriber import Transcriber
    from syncsage.sync.connectors import ConnectorItem, ConnectorPayload

CAPTION_SIDECAR_SUFFIX = ".caption.txt"
TRANSCRIPT_SIDECAR_SUFFIX = ".transcript.txt"


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
    """Files a source would index, via the shared pruning walk.

    Delegates to :func:`syncsage.ingestion.walk.walk_source` so there is one
    traversal implementation rather than two that drift — this used to
    ``rglob("*")`` the whole tree and filter afterwards, which meant exclude
    patterns never pruned the walk.
    """
    from syncsage.ingestion.walk import WalkBudget, walk_source

    report = walk_source(
        source.path,
        include=source.include,
        exclude=source.exclude,
        max_depth=source.max_depth,
        budget=WalkBudget.from_settings(source.limits),
        follow_symlinks=bool(getattr(source.limits, "follow_symlinks", False)),
    )
    return report.files


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


def _sidecar_for_path(path: Path, suffix: str = CAPTION_SIDECAR_SUFFIX) -> bytes | None:
    sidecar = path.with_name(path.name + suffix)
    try:
        return sidecar.read_bytes()
    except OSError:
        return None


def caption_to_text(
    captioner: Captioner | None,
    content: bytes,
    relative_path: str,
    sidecar: bytes | None,
) -> str:
    """Caption image bytes into indexable text (architecture §8).

    With no captioner configured we fall back to the file name so an
    image-bearing source still produces *some* deterministic indexable text
    rather than an empty (skipped) artifact.
    """

    if captioner is None:
        stem = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
        return f"Image {stem}." if stem else "Image."
    try:
        return captioner.caption(content, relative_path, sidecar=sidecar)  # type: ignore[call-arg]
    except TypeError:
        # A captioner without the optional ``sidecar`` kwarg (duck-typed).
        return captioner.caption(content, relative_path)


def transcribe_to_text(
    transcriber: Transcriber | None,
    content: bytes,
    relative_path: str,
    sidecar: bytes | None,
) -> str:
    """Transcribe audio bytes into indexable text (architecture §8).

    The audio twin of :func:`caption_to_text`. With no transcriber configured
    we fall back to the file name so an audio-bearing source still produces
    *some* deterministic indexable text rather than an empty (skipped) artifact.
    """

    if transcriber is None:
        stem = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
        return f"Audio {stem}." if stem else "Audio."
    try:
        return transcriber.transcribe(content, relative_path, sidecar=sidecar)  # type: ignore[call-arg]
    except TypeError:
        # A transcriber without the optional ``sidecar`` kwarg (duck-typed).
        return transcriber.transcribe(content, relative_path)


def parse_file(
    source: SourceConfig,
    path: Path,
    git_metadata: tuple[str | None, str | None, bool] | None = None,
    captioner: Captioner | None = None,
    transcriber: Transcriber | None = None,
) -> ParsedArtifact | None:
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS | {".pdf", ".docx"} | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS:
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
    if suffix in IMAGE_EXTENSIONS:
        text = caption_to_text(captioner, path.read_bytes(), relative, _sidecar_for_path(path))
    elif suffix in AUDIO_EXTENSIONS:
        text = transcribe_to_text(
            transcriber,
            path.read_bytes(),
            relative,
            _sidecar_for_path(path, TRANSCRIPT_SIDECAR_SUFFIX),
        )
    else:
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
    captioner: Captioner | None = None,
    transcriber: Transcriber | None = None,
) -> ParsedArtifact | None:
    suffix = Path(item.relative_path).suffix.lower()
    mime_type = payload.mime_type or item.mime_type
    is_image = suffix in IMAGE_EXTENSIONS
    is_audio = suffix in AUDIO_EXTENSIONS
    if (
        suffix not in TEXT_EXTENSIONS | {".pdf", ".docx"}
        and not is_image
        and not is_audio
        and not _is_text_like(mime_type)
    ):
        return None
    branch, commit, _dirty = (
        git_metadata
        if git_metadata is not None
        else git_state(source.path)
        if source.type.value == "repository"
        else (None, None, False)
    )
    content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
    if is_image:
        text = caption_to_text(
            captioner,
            payload.content,
            item.relative_path,
            _sidecar_for_payload(payload),
        )
    elif is_audio:
        text = transcribe_to_text(
            transcriber,
            payload.content,
            item.relative_path,
            _sidecar_for_payload(payload, TRANSCRIPT_SIDECAR_SUFFIX),
        )
    else:
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


def _sidecar_for_payload(
    payload: ConnectorPayload, suffix: str = CAPTION_SIDECAR_SUFFIX
) -> bytes | None:
    """Read an authored caption/transcript sidecar for a filesystem payload."""

    path = payload.metadata.get("path")
    if not path:
        return None
    return _sidecar_for_path(Path(path), suffix)


def _is_text_like(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    return mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/xml",
        "application/x-yaml",
    }
