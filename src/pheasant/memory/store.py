"""Append-only agent-memory record store (Product Framework Step 33.1).

The load-bearing design decision: **memory records are source content**.
A record is one Markdown file under the memory source's directory —
frontmatter above the memory text — and indexing it is the ordinary
deterministic chunk→embed→graph pipeline over a filesystem source. The
write path below only ever *creates files*; it never touches the indexing
path, so the no-LLM/determinism pillar and the engine's sha256 idempotency
apply to memory exactly as to any other source.

Record ids are deterministic: ``mem-<asserted-at>-<blake2b8(scope|subject|text)>``.
An identical write (same text/scope/subject at the same instant) maps to
the same file and is reported as not-created — append-only, nothing is
ever overwritten. Re-asserting a fact at a later time creates a new
record; recency is signal, and consolidation of supersedes-chains is Step
33.2's job.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType

MEMORY_RECORD_VERSION = 1
VALID_SCOPES = ("session", "user", "org")

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def memory_source(config: PheasantConfig) -> SourceConfig | None:
    """The first enabled ``type: memory`` source, or None."""
    for source in config.sources:
        if source.enabled and source.type == SourceType.memory:
            return source
    return None


@dataclass(frozen=True)
class ConsolidationReport:
    """What one consolidation pass archived (Step 33.2). Deterministic in ``now``."""

    archived_superseded: tuple[str, ...]
    archived_expired: tuple[str, ...]
    kept: int

    @property
    def archived(self) -> int:
        return len(self.archived_superseded) + len(self.archived_expired)

    def as_dict(self) -> dict[str, object]:
        return {
            "archived_superseded": list(self.archived_superseded),
            "archived_expired": list(self.archived_expired),
            "archived": self.archived,
            "kept": self.kept,
        }


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    scope: str
    subject: str | None
    text: str
    asserted_at: str
    supersedes: str | None
    tags: tuple[str, ...]
    path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "scope": self.scope,
            "subject": self.subject,
            "text": self.text,
            "asserted_at": self.asserted_at,
            "supersedes": self.supersedes,
            "tags": list(self.tags),
            "path": str(self.path),
            "schema_version": MEMORY_RECORD_VERSION,
        }


class MemoryStore:
    """One record per Markdown file under ``root/<scope>/<record_id>.md``."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def append(
        self,
        text: str,
        *,
        scope: str = "user",
        subject: str | None = None,
        supersedes: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, bool]:
        """Write one memory record; returns ``(record, created)``.

        Append-only: an existing record file is never rewritten — an
        identical write returns the stored record with ``created=False``.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("memory text must be a non-empty string")
        if scope not in VALID_SCOPES:
            raise ValueError(f"memory scope must be one of {VALID_SCOPES}, got {scope!r}")
        instant = (now or datetime.now(tz=UTC)).astimezone(UTC).replace(microsecond=0)
        asserted_at = instant.isoformat().replace("+00:00", "Z")
        digest = hashlib.blake2b(
            f"{scope}|{subject or ''}|{text}".encode(), digest_size=8
        ).hexdigest()
        record_id = f"mem-{instant.strftime('%Y%m%dT%H%M%SZ')}-{digest}"
        path = self.root / scope / f"{record_id}.md"
        record = MemoryRecord(
            record_id=record_id,
            scope=scope,
            subject=subject,
            text=text,
            asserted_at=asserted_at,
            supersedes=supersedes,
            tags=tuple(tags),
            path=path,
        )
        if path.exists():
            return self.load(path), False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(_render(record), encoding="utf-8")
        os.replace(tmp, path)
        return record, True

    def load(self, path: Path) -> MemoryRecord:
        raw = Path(path).read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(raw)
        if not match:
            raise ValueError(f"not a memory record (missing frontmatter): {path}")
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        tags = tuple(t.strip() for t in fields.get("tags", "").split(",") if t.strip())
        return MemoryRecord(
            record_id=fields.get("record_id", Path(path).stem),
            scope=fields.get("memory_scope", "user"),
            subject=fields.get("memory_subject") or None,
            text=raw[match.end() :].strip(),
            asserted_at=fields.get("asserted_at", ""),
            supersedes=fields.get("supersedes") or None,
            tags=tags,
            path=Path(path),
        )

    def list_records(
        self, scope: str | None = None, *, current_only: bool = False
    ) -> list[MemoryRecord]:
        """Records sorted by (asserted_at, id); ``current_only`` drops superseded ones.

        Validity chains (Step 33.2): a record is *superseded* — no longer
        current — as soon as any record names it in ``supersedes``. The chain
        is resolved across every scope so a cross-scope correction still
        invalidates its target.
        """
        if scope is not None and scope not in VALID_SCOPES:
            raise ValueError(f"memory scope must be one of {VALID_SCOPES}, got {scope!r}")
        everything: list[MemoryRecord] = []
        for name in VALID_SCOPES:
            scope_dir = self.root / name
            if not scope_dir.is_dir():
                continue
            for path in sorted(scope_dir.glob("mem-*.md")):
                everything.append(self.load(path))
        if current_only:
            superseded = self.superseded_ids(everything)
            everything = [r for r in everything if r.record_id not in superseded]
        if scope is not None:
            everything = [r for r in everything if r.scope == scope]
        everything.sort(key=lambda r: (r.asserted_at, r.record_id))
        return everything

    @staticmethod
    def superseded_ids(records: list[MemoryRecord]) -> set[str]:
        """Every record id named in some record's ``supersedes`` field."""
        return {r.supersedes for r in records if r.supersedes}

    def consolidate(
        self,
        *,
        now: datetime | None = None,
        session_ttl_days: int | None = None,
        user_ttl_days: int | None = None,
        org_ttl_days: int | None = None,
    ) -> ConsolidationReport:
        """Archive superseded and TTL-expired records (Step 33.2).

        Archiving renames ``<id>.md`` to ``<id>.md.archived`` in place — the
        bytes are preserved forever (append-only), but the file stops matching
        the memory source's ``**/*.md`` include glob, so the next **full**
        sync of the source drops it from the index. Deterministic in ``now``
        and idempotent: a second pass over unchanged content archives nothing.
        TTLs are opt-in per scope (``None`` = that scope never expires).
        """
        instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
        ttls = {"session": session_ttl_days, "user": user_ttl_days, "org": org_ttl_days}
        records = self.list_records()
        superseded = self.superseded_ids(records)
        archived_superseded: list[str] = []
        archived_expired: list[str] = []
        for record in records:
            if record.record_id in superseded:
                self._archive(record)
                archived_superseded.append(record.record_id)
                continue
            ttl_days = ttls.get(record.scope)
            if ttl_days is None or not record.asserted_at:
                continue
            asserted = datetime.fromisoformat(record.asserted_at.replace("Z", "+00:00"))
            if (instant - asserted).days >= ttl_days:
                self._archive(record)
                archived_expired.append(record.record_id)
        kept = len(records) - len(archived_superseded) - len(archived_expired)
        return ConsolidationReport(
            archived_superseded=tuple(sorted(archived_superseded)),
            archived_expired=tuple(sorted(archived_expired)),
            kept=kept,
        )

    @staticmethod
    def _archive(record: MemoryRecord) -> None:
        os.replace(record.path, record.path.parent / (record.path.name + ".archived"))


def _render(record: MemoryRecord) -> str:
    lines = [
        "---",
        f"schema_version: {MEMORY_RECORD_VERSION}",
        f"record_id: {record.record_id}",
        f"memory_scope: {record.scope}",
    ]
    if record.subject:
        lines.append(f"memory_subject: {record.subject}")
    lines.append(f"asserted_at: {record.asserted_at}")
    if record.supersedes:
        lines.append(f"supersedes: {record.supersedes}")
    if record.tags:
        lines.append(f"tags: {', '.join(record.tags)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + record.text + "\n"
