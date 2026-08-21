"""Append-only agent-memory record store (Product Framework Step 33.1).

The load-bearing design decision: **memory records are source content**.
A record is one Markdown file under the memory source's directory —
frontmatter above the memory text — and indexing it is the ordinary
deterministic chunk→embed→graph pipeline over a filesystem source. The
write path below only ever *creates files*; it never touches the indexing
path, so the no-LLM/determinism pillar and the engine's sha256 idempotency
apply to memory exactly as to any other source.

Record ids are deterministic: ``mem-<asserted-at>-<blake2b8(scope|subject|text)>``.
An identical write is reported as not-created — append-only, nothing is
ever overwritten. Identity is the **digest**, not the timestamp: re-asserting
the same thing returns the record already stored however much later it
happens (Step 33.5; before that it deduplicated only within the same
wall-clock second, which was a race rather than a rule). A write that
differs in any way the digest covers — corrected text, a different writer,
kind or validity window — is a genuinely new assertion and gets its own
record. Consolidation of supersedes-chains is Step 33.2's job.

Schema version 2 (Step 33.5) adds ``kind``, ``written_by``, ``valid_from``
and ``valid_until``. **Version 1 records stay valid forever** — nothing on
disk is rewritten (rule 2), :func:`MemoryStore.load` fills the new fields
with their v1 meanings, and the id digest only grows extra parts for
values a v1 record could not have carried, so **every v1 record id is
reproduced byte-identically**. See :func:`_digest_input`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType

logger = logging.getLogger(__name__)

MEMORY_RECORD_VERSION = 2
VALID_SCOPES = ("session", "user", "org")

#: What a record *is*. ``fact`` is an assertion to recall; the other three are
#: retrieval policy, read by Step 33.8's steering loader. The distinction is
#: stored from 33.5 so enabling steering later needs no second schema bump.
DEFAULT_KIND = "fact"
VALID_KINDS = ("fact", "alias", "preference", "exclusion")

#: ``\r?`` is load-bearing, not defensive. ``_render`` joins with ``\n`` but
#: ``Path.write_text`` translates to the platform newline, so on Windows every
#: record is stored CRLF. ``load`` never noticed — ``read_text`` translates
#: back — but the *indexing* path decodes the bytes itself, so a ``\n``-only
#: pattern silently failed to match there and the frontmatter went on being
#: indexed as prose on exactly the platform it was written on.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)

#: A line break in any frontmatter value forges frontmatter. ``_render`` writes
#: one ``key: value`` per line and :meth:`MemoryStore.load` reads them back the
#: same way, so a newline smuggled into a caller-supplied field appends
#: arbitrary *keys* to the block. That is not a formatting nuisance:
#: ``memory_scope`` is what :func:`pheasant.security.acl.normalize_acl` reads to
#: build a record's read ACL, so ``subject="x\nmemory_scope: org"`` turns a
#: private ``user`` note into a world-readable ``org`` one under
#: ``security.acl_enforced`` — reopening the very leak Step 33.11 closed, this
#: time through input rather than through a missing rule. ``supersedes`` is the
#: other prize: forging it invalidates any record whose id the writer can guess.
#: Rejected rather than escaped, because no legitimate subject, tag or writer id
#: contains a line break, and a value that survives a round trip unchanged is
#: worth more here than one that is silently mangled into safety.
_FORBIDDEN_IN_FIELD = ("\n", "\r")


def _checked_field(name: str, value: str | None) -> str | None:
    """A frontmatter-safe scalar, or raise. ``None`` passes through."""
    if value is None:
        return None
    text = str(value)
    if any(char in text for char in _FORBIDDEN_IN_FIELD):
        raise ValueError(f"memory {name} must not contain a line break")
    return text


def _checked_instant(name: str, value: str | None) -> str | None:
    """A frontmatter-safe ISO-8601 instant, or raise. ``None`` passes through.

    Validity is compared as a *string* everywhere downstream (``admits``,
    ``sql_predicate`` and ``effective_valid_until`` all rely on ISO-8601 in Z
    form sorting lexicographically). An unparseable value therefore does not
    fail loudly — it sorts above every real instant, so ``valid_until="soon"``
    silently means "never expires". Checked at the door instead.
    """
    text = _checked_field(name, value)
    if text is None:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"memory {name} must be an ISO-8601 instant, got {value!r}") from None
    return text


def strip_frontmatter(text: str) -> tuple[str, int]:
    """Split a record's frontmatter off its body. Returns ``(body, lines_removed)``.

    The frontmatter was being indexed as prose: ``record_id``, ISO timestamps
    and ``schema_version`` all became BM25 terms, so every record carried a
    handful of high-cardinality tokens that match nothing a person would ask
    while diluting the terms that do.

    ``lines_removed`` exists because :func:`~pheasant.ingestion.chunking.chunk_text`
    derives ``start_line``/``end_line`` from offsets into the text it is handed.
    Dropping the frontmatter without telling the caller how many lines went with
    it would silently re-point every chunk's provenance at the wrong line of the
    real file, so callers add it back — the same absolute-line correction
    ``_section_aligned_chunks`` already applies.

    The blank separator line is consumed too, and counted. ``chunk_text``
    ``strip()``s each slice while computing ``start_line`` from the *unstripped*
    offset, so leaving the blank line in would report a chunk as starting one
    line above its own first word. The invariant callers get: the returned text
    begins at the first content character, and ``lines_removed`` is exactly how
    many lines of the file precede it.
    """
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return text, 0
    body = text[match.end() :]
    content = body.lstrip("\r\n")
    consumed = body[: len(body) - len(content)]
    return content, match.group(0).count("\n") + consumed.count("\n")


def _digest_input(
    scope: str,
    subject: str | None,
    text: str,
    kind: str,
    written_by: str | None,
    valid_from: str,
    valid_until: str | None,
    asserted_at: str,
) -> str:
    """The string hashed into a record id.

    Backward compatibility is the whole design here. A v1 record was hashed
    over exactly ``scope|subject|text``, so this must reproduce that string
    for anything a v1 write could have expressed — otherwise every existing
    record's id changes, the file on disk stops matching its own frontmatter,
    and the append-only guarantee turns into silent duplication.

    So the extra parts are appended **only when they differ from what v1
    implied**. They are appended at all because each one genuinely
    distinguishes an assertion: the same sentence stored as an ``alias``
    rather than a ``fact`` is a different record, and — load-bearing for the
    per-principal isolation in 33.11 — the same sentence written by two
    principals in the same second must not collide into one file owned by
    whoever got there first.
    """
    base = f"{scope}|{subject or ''}|{text}"
    extras: list[str] = []
    if kind != DEFAULT_KIND:
        extras.append(f"kind={kind}")
    if written_by:
        extras.append(f"by={written_by}")
    if valid_from and valid_from != asserted_at:
        extras.append(f"from={valid_from}")
    if valid_until:
        extras.append(f"until={valid_until}")
    return base + ("|" + "|".join(extras) if extras else "")


def memory_source(config: PheasantConfig, state: object | None = None) -> SourceConfig | None:
    """The first enabled ``type: memory`` source, or None.

    ``state`` enables the same **runtime-registry fallback** ``SyncEngine._source``
    has: a source created through the API (``POST /memory/enable``, the UI's
    Enable-memory button) lives in the state registry and only reaches
    ``config.sources`` in the process that created it. Without the fallback,
    memory enabled from the UI is invisible to every *other* process — a fresh
    MCP stdio server, the sync worker subprocess, the container after a
    restart — and `describe_retrieval` truthfully reports no memory at all.
    Found on a live run, where enabling worked and then the region said it had
    no memory source.

    Config wins when it has one, so a YAML-declared source is never shadowed.
    """
    for source in config.sources:
        if source.enabled and source.type == SourceType.memory:
            return source
    if state is None:
        return None
    try:
        rows = state.rows(  # type: ignore[attr-defined]
            "SELECT config_json FROM sources WHERE type='memory' AND enabled=1 ORDER BY name"
        )
    except Exception:  # pragma: no cover - defensive; a missing table is not fatal
        return None
    for row in rows:
        try:
            import json

            payload = json.loads(row["config_json"])
            resolved = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
        except Exception:  # pragma: no cover - a malformed row is not fatal
            continue
        # Cached on the live config so repeat lookups (and everything else that
        # reads `config.sources`) see it too, exactly as `_source` does.
        config.sources.append(resolved)
        return resolved
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
    # Schema 2 (Step 33.5). Defaulted so that positional construction from
    # before 33.5 still works, and so a v1 file on disk loads without a
    # rewrite: kind="fact", nobody recorded as the writer, valid from the
    # instant it was asserted, and open-ended.
    kind: str = DEFAULT_KIND
    written_by: str | None = None
    valid_from: str = ""
    valid_until: str | None = None

    #: Version of the on-disk record this instance was read from. A v1 file
    #: keeps reporting 1 — `as_dict` must not claim a record carries fields it
    #: does not, or a reader cannot tell a real `valid_until` from a default.
    schema_version: int = MEMORY_RECORD_VERSION

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
            "schema_version": self.schema_version,
            "kind": self.kind,
            "written_by": self.written_by,
            "valid_from": self.valid_from or self.asserted_at,
            "valid_until": self.valid_until,
        }


class MemoryStore:
    """One record per Markdown file under ``root/<scope>/<record_id>.md``."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        # Parse-once cache for one maintenance pass (Phase 0). Keyed on
        # (mtime_ns, size), not just "seen before": records are append-only
        # so this is exact rather than heuristic — a cache hit means the
        # bytes on disk cannot have changed since the entry was made. This
        # is what lets `run_memory_maintenance` call `list_records()` twice
        # (once inside `consolidate`, once for the capacity-prune archive
        # loop) at the cost of one `stat()` per file on the second call
        # instead of a second full parse of the store.
        self._record_cache: dict[Path, tuple[int, int, MemoryRecord]] = {}

    def append(
        self,
        text: str,
        *,
        scope: str = "user",
        subject: str | None = None,
        supersedes: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
        kind: str = DEFAULT_KIND,
        written_by: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
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
        if kind not in VALID_KINDS:
            raise ValueError(f"memory kind must be one of {VALID_KINDS}, got {kind!r}")
        # Every caller-supplied value that reaches the frontmatter is checked
        # here, before it can reach `_render` *or* the id digest — see
        # `_FORBIDDEN_IN_FIELD`. `scope` and `kind` are already closed sets
        # above, and `asserted_at`/`record_id` are generated below.
        subject = _checked_field("subject", subject)
        supersedes = _checked_field("supersedes", supersedes)
        written_by = _checked_field("written_by", written_by)
        tags = tuple(str(_checked_field("tag", tag)) for tag in tags)
        instant = (now or datetime.now(tz=UTC)).astimezone(UTC).replace(microsecond=0)
        asserted_at = instant.isoformat().replace("+00:00", "Z")
        valid_from = _checked_instant("valid_from", valid_from) or asserted_at
        valid_until = _checked_instant("valid_until", valid_until)
        digest = hashlib.blake2b(
            _digest_input(
                scope, subject, text, kind, written_by, valid_from, valid_until, asserted_at
            ).encode(),
            digest_size=8,
        ).hexdigest()
        record_id = f"mem-{instant.strftime('%Y%m%dT%H%M%SZ')}-{digest}"
        path = self.root / scope / f"{record_id}.md"
        # Content identity is the *digest*; the timestamp prefix orders and
        # labels records, it does not identify them. Matching on the full path
        # alone made "an identical write returns the stored record" true only
        # when both writes landed in the same wall-clock second — so the same
        # assertion deduplicated or duplicated depending on how long the
        # intervening sync took. Nobody means "within one second" as a
        # semantic boundary; that was a race, and it is the cause of the
        # long-standing flake in
        # `test_memory_write_is_searchable_immediately_and_resync_is_zero_work`.
        #
        # Anything that genuinely differs still makes a new record, and 33.5
        # widened that set: a correction, a different writer, a different kind
        # or a different validity window all change the digest. An *archived*
        # record no longer matches, so re-asserting something that was
        # consolidated away correctly brings it back.
        existing = next(iter(sorted((self.root / scope).glob(f"mem-*-{digest}.md"))), None)
        if existing is not None:
            return self.load(existing), False
        record = MemoryRecord(
            record_id=record_id,
            scope=scope,
            subject=subject,
            text=text,
            asserted_at=asserted_at,
            supersedes=supersedes,
            tags=tuple(tags),
            path=path,
            kind=kind,
            written_by=written_by,
            valid_from=valid_from,
            valid_until=valid_until,
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
            # **First occurrence wins**, deliberately. `_render` writes each key
            # exactly once and in a fixed order, so a duplicate can only come
            # from a value that contained a line break — and last-wins is what
            # let such a value *override* the real `memory_scope` rather than
            # merely appear after it. Writes are now rejected at the door
            # (`_checked_field`); this keeps any record already on disk from a
            # pre-fix release from being read at the forged scope.
            fields.setdefault(key.strip(), value.strip())
        tags = tuple(t.strip() for t in fields.get("tags", "").split(",") if t.strip())
        asserted_at = fields.get("asserted_at", "")
        try:
            version = int(fields.get("schema_version", "1"))
        except ValueError:
            version = 1
        return MemoryRecord(
            record_id=fields.get("record_id", Path(path).stem),
            scope=fields.get("memory_scope", "user"),
            subject=fields.get("memory_subject") or None,
            text=raw[match.end() :].strip(),
            asserted_at=asserted_at,
            supersedes=fields.get("supersedes") or None,
            tags=tags,
            path=Path(path),
            # v1 defaults, spelled out: an ordinary fact, no recorded writer,
            # true from the moment it was asserted, open-ended.
            kind=fields.get("kind") or DEFAULT_KIND,
            written_by=fields.get("written_by") or None,
            valid_from=fields.get("valid_from") or asserted_at,
            valid_until=fields.get("valid_until") or None,
            schema_version=version,
        )

    def _load_cached(self, path: Path) -> MemoryRecord:
        """`load(path)`, reusing the last parse if the file is unchanged.

        Exact, not heuristic: a hit requires both `st_mtime_ns` and `st_size`
        to match the cached entry, and records are never rewritten in place
        (append-only), so a stale hit cannot happen within one process.
        """
        stat = path.stat()
        cached = self._record_cache.get(path)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        record = self.load(path)
        self._record_cache[path] = (stat.st_mtime_ns, stat.st_size, record)
        return record

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
                try:
                    everything.append(self._load_cached(path))
                except (ValueError, OSError):
                    # One unreadable file must not cost the caller everything
                    # else. This method backs listing, consolidation *and* the
                    # 33.5 projection, so raising meant a single stray or
                    # half-written file in the memory directory took down all
                    # three — the same reason a malformed document contributes
                    # no text instead of aborting a whole sync. Logged, never
                    # silent, and the file itself is left untouched.
                    logger.warning("skipping unreadable memory record: %s", path, exc_info=True)
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
        records: list[MemoryRecord] | None = None,
    ) -> ConsolidationReport:
        """Archive superseded and TTL-expired records (Step 33.2).

        Archiving renames ``<id>.md`` to ``<id>.md.archived`` in place — the
        bytes are preserved forever (append-only), but the file stops matching
        the memory source's ``**/*.md`` include glob, so the next **full**
        sync of the source drops it from the index. Deterministic in ``now``
        and idempotent: a second pass over unchanged content archives nothing.
        TTLs are opt-in per scope (``None`` = that scope never expires).

        ``records``, when given, is used instead of a fresh
        :meth:`list_records` call — a caller that runs a second store
        operation in the same maintenance pass (capacity pruning) can parse
        the store once and hand the list to both (Phase 0).
        """
        instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
        ttls = {"session": session_ttl_days, "user": user_ttl_days, "org": org_ttl_days}
        records = self.list_records() if records is None else records
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
    def archive(record: MemoryRecord) -> None:
        """Rename a record out of the include glob. Bytes are never deleted.

        Public because capacity pruning (Step 33.9) archives on a different
        trigger than consolidation — the store is over its bound rather than
        the record being corrected or expired — but must forget in exactly the
        same way.
        """
        os.replace(record.path, record.path.parent / (record.path.name + ".archived"))

    #: Pre-33.9 name, kept because it is used throughout this module.
    _archive = archive


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
    # Schema 2. Each is written only when it says something a reader could not
    # have assumed, so a plain fact still renders exactly the v1 way and the
    # file stays as small as it was.
    if record.kind != DEFAULT_KIND:
        lines.append(f"kind: {record.kind}")
    if record.written_by:
        lines.append(f"written_by: {record.written_by}")
    if record.valid_from and record.valid_from != record.asserted_at:
        lines.append(f"valid_from: {record.valid_from}")
    if record.valid_until:
        lines.append(f"valid_until: {record.valid_until}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + record.text + "\n"
