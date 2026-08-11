"""Query-time memory policy (Step 33.6).

Before this, memory was invisible at retrieval time. A memory chunk looked like
any other Markdown note, the only lever was `exclude_sources=["<name>"]` — MCP
only, and only if the caller already knew the source's configured name — and a
*superseded* record stayed retrievable until the batch consolidation pass
archived it and a full re-sync ran. That last one is the stale-fact error the
agent-memory literature names as the primary defect: the region knows the fact
was corrected and serves the old one anyway.

`MemoryPolicy` is the one knob, spelled identically on every protocol. It is
optional everywhere and its default reproduces the pre-33.6 behavior, except
that a corrected record is no longer returned — which is a bug fix, not a
policy choice.

**One rule, two encodings.** Validity is filtered in SQL for the text arm
(post-filtering a globally-ranked page can return nothing from a narrow slice
while its rows sit just past the cut) and in Python for the vector and graph
arms. :func:`sql_predicate` and :func:`admits` therefore have to agree exactly,
so they live side by side here and `tests/test_memory_policy.py` asserts they
return the same answer over a matrix of records — the same "one module owns the
normalization" arrangement as `section_needle`/`section_matches`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: `auto`   — memory participates like any other source (the default).
#: `off`    — no memory in the results at all.
#: `only`   — memory and nothing else.
#: `prefer` — memory participates, and is guaranteed a share of the slots it
#:            would otherwise have been truncated out of.
VALID_MODES = ("auto", "off", "only", "prefer")
DEFAULT_MODE = "auto"

#: Fraction of `max_results` reserved for memory under `prefer`. Half: enough
#: that remembered context cannot be crowded out by a large corpus, not so much
#: that the corpus stops being the answer.
PREFER_SHARE = 0.5


@dataclass(frozen=True)
class MemoryPolicy:
    """How one query treats the region's agent memory."""

    mode: str = DEFAULT_MODE
    scopes: tuple[str, ...] | None = None
    subject: str | None = None
    #: Drop records a later record has corrected. On by default: serving a
    #: fact the region *knows* was superseded is never what the caller wanted.
    current_only: bool = True
    #: Point-in-time recall — "what did we believe then". Implies the validity
    #: window is evaluated at that instant instead of now, which is what makes
    #: a superseded record visible again rather than merely unfiltered.
    as_of: str | None = None
    #: Cap on how many of the returned slots memory may occupy.
    max_results: int | None = None

    @property
    def is_default(self) -> bool:
        """True when this policy asks for nothing the pre-33.6 code did not do.

        Used to keep a region without memory on exactly its old code path.
        """
        return (
            self.mode == DEFAULT_MODE
            and not self.scopes
            and not self.subject
            and self.as_of is None
            and self.max_results is None
        )

    @classmethod
    def parse(cls, value: str | Mapping[str, Any] | MemoryPolicy | None) -> MemoryPolicy:
        """Accept the shorthand string, a mapping, or an existing policy.

        `"off"` is the common case and deserves to be one word; the mapping
        form exists for the rest. An unknown mode falls back to the default
        rather than raising: a retrieval hint is not worth failing a search
        over, and the same forgiving treatment is what `mode` already gets in
        `HybridSearch.search_context`.
        """
        if value is None:
            return cls()
        if isinstance(value, MemoryPolicy):
            return value
        if isinstance(value, str):
            mode = value.strip().lower()
            return cls(mode=mode if mode in VALID_MODES else DEFAULT_MODE)
        if not isinstance(value, Mapping):
            return cls()

        mode = str(value.get("mode") or DEFAULT_MODE).strip().lower()
        raw_scopes = value.get("scopes")
        if isinstance(raw_scopes, str):
            raw_scopes = [raw_scopes]
        scopes = tuple(str(scope) for scope in raw_scopes) if raw_scopes else None
        max_results = value.get("max_results")
        return cls(
            mode=mode if mode in VALID_MODES else DEFAULT_MODE,
            scopes=scopes,
            subject=(str(value["subject"]) if value.get("subject") else None),
            current_only=bool(value.get("current_only", True)),
            as_of=(str(value["as_of"]) if value.get("as_of") else None),
            max_results=(int(max_results) if max_results is not None else None),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scopes": list(self.scopes) if self.scopes else None,
            "subject": self.subject,
            "current_only": self.current_only,
            "as_of": self.as_of,
            "max_results": self.max_results,
        }


def utc_now_iso() -> str:
    """The instant validity is evaluated at, in the format records store."""
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validity_instant(policy: MemoryPolicy, now: str) -> str | None:
    """When to evaluate a record's validity window, or None for no filter.

    `as_of` always wins — asking what was believed on a date is meaningless if
    the answer is silently clamped to what is current. Otherwise the window is
    applied only when `current_only` is on, so switching it off returns the
    full history including corrected records.
    """
    if policy.as_of:
        return policy.as_of
    return now if policy.current_only else None


def admits(policy: MemoryPolicy, record: Mapping[str, Any] | None, *, now: str) -> bool:
    """Does this policy allow a hit through? `record=None` means "not a memory".

    The Python half of the rule. Kept byte-for-byte equivalent to
    :func:`sql_predicate`; see the module docstring.
    """
    if record is None:
        return policy.mode != "only"
    if policy.mode == "off":
        return False
    if policy.scopes and str(record.get("scope") or "") not in policy.scopes:
        return False
    if policy.subject:
        needle = policy.subject.strip().lower()
        if needle and needle not in str(record.get("subject") or "").lower():
            return False
    instant = validity_instant(policy, now)
    if instant is not None:
        valid_from = str(record.get("valid_from") or "")
        valid_until = str(record.get("valid_until") or "")
        if valid_from and valid_from > instant:
            return False
        if valid_until and valid_until <= instant:
            return False
    return True


def sql_predicate(
    policy: MemoryPolicy, *, now: str, alias: str = "memory_records"
) -> tuple[str, list[Any]]:
    """The SQL half of the rule, for a LEFT JOIN against `memory_records`.

    Returns `(condition, params)`. A row with `record_id IS NULL` is not a
    memory record; under every mode but `only` it passes untouched, which is
    what keeps a scope or subject filter from also narrowing the corpus.
    """
    if policy.mode == "off":
        return f"{alias}.record_id IS NULL", []

    clauses: list[str] = []
    params: list[Any] = []
    if policy.scopes:
        placeholders = ",".join("?" for _ in policy.scopes)
        clauses.append(f"{alias}.scope IN ({placeholders})")
        params.extend(policy.scopes)
    if policy.subject:
        needle = policy.subject.strip().lower()
        if needle:
            clauses.append(f"LOWER(COALESCE({alias}.subject, '')) LIKE ?")
            params.append(f"%{needle}%")
    instant = validity_instant(policy, now)
    if instant is not None:
        # COALESCE on both sides so an empty string means "unset", exactly as
        # `admits` reads it. Without it, `'' > instant` is false in SQL and
        # true-ish in Python and the two halves disagree on a corner case.
        clauses.append(f"(COALESCE({alias}.valid_from, '') = '' OR {alias}.valid_from <= ?)")
        params.append(instant)
        clauses.append(f"(COALESCE({alias}.valid_until, '') = '' OR {alias}.valid_until > ?)")
        params.append(instant)

    qualifies = " AND ".join(clauses) if clauses else "1=1"
    if policy.mode == "only":
        return f"({alias}.record_id IS NOT NULL AND {qualifies})", params
    return f"({alias}.record_id IS NULL OR ({qualifies}))", params


#: Columns the projection exposes to retrieval. Selected explicitly rather than
#: `SELECT *` so adding a column (33.9's counters) cannot silently widen what a
#: search result reports about a memory.
INDEX_COLUMNS = (
    "artifact_id, source_id, record_id, scope, subject, kind, asserted_at, valid_from, valid_until"
)


#: Separator for the composite `(source_id, relative_path)` key. NUL cannot
#: occur in either half, so the two can never be confused for one another.
_PATH_KEY = "\x00"


def load_memory_index(state: Any) -> dict[str, dict[str, Any]]:
    """Every memory record, keyed by each identity a search hit might carry.

    One small query per search. The table holds one row per memory record — a
    handful in practice, and bounded outright by `memory.max_records` — so
    loading it is cheaper than joining it three times, and it lets the vector
    and graph arms share the text arm's rule instead of approximating it.

    Two keys per record, because hits do not agree on how they name things: a
    chunk or artifact hit carries the **artifact id**, while a graph hit is a
    *chunk node* whose id is `chunk:…` and whose `relative_path` is null. Keying
    only by artifact id silently admitted every graph-arm hit — a corrected
    record came back through the graph while being correctly filtered out of
    the text arm.
    """
    # A store need not have one: `HybridSearch` accepts any object exposing
    # `search`, and has always been constructed that way in tests and by
    # callers that only want the graph arm. No state means no memory, which is
    # a perfectly ordinary answer rather than an error.
    if state is None:
        return {}
    try:
        rows = state.rows(f"SELECT {INDEX_COLUMNS} FROM memory_records")
    except Exception:  # pragma: no cover - a state store older than 33.5
        return {}
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        artifact_id = str(record.get("artifact_id") or "")
        index[artifact_id] = record
        relative_path = relative_path_of(artifact_id)
        source_id = record.get("source_id") or ""
        if relative_path:
            index[f"{source_id}{_PATH_KEY}{relative_path}"] = record
    return index


def relative_path_of(stable_id: str) -> str:
    """The relative path inside an artifact or chunk stable id, or `""`.

    The grammars are contracts (`docs/graph_model.md`, CLAUDE.md rule 3):

        file:{source}:{relpath}:branch={b}
        chunk:{source}:{relpath}:sha256={hash}:chunk={index}

    Both put the path third, so the path is what sits between the second colon
    and the trailing `:branch=` / `:sha256=` marker. Split on those markers
    rather than on colon count, since a path may legitimately contain a colon.
    """
    parts = stable_id.split(":", 2)
    if len(parts) < 3:
        return ""
    tail = parts[2]
    for marker in (":sha256=", ":branch="):
        if marker in tail:
            return tail.rsplit(marker, 1)[0]
    return tail


def resolve(item: Mapping[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The memory record a search hit belongs to, or None if it is not one."""
    if not index:
        return None
    node_id = str(item.get("node_id") or "")
    record = index.get(node_id)
    if record is not None:
        return record
    # A node *derived* from an artifact — a chunk, symbol or entity — names the
    # artifact it came from. This is what lets the policy judge an entity label
    # extracted out of a memory record, which before Step 33.7 exposed no
    # identity at all and so survived a filter that correctly dropped the
    # record itself.
    derived_from = item.get("artifact_id")
    if derived_from:
        record = index.get(str(derived_from))
        if record is not None:
            return record
    # A relationship hit is one edge, and it belongs to memory if *either* end
    # does — `entity:…:bananas --derived_from--> <the record>` carries the
    # record's content just as surely as the record's own chunk. Its `node_id`
    # is the edge's source node, so neither of the lookups above sees the
    # target at all.
    for endpoint in ("target", "source"):
        value = item.get(endpoint)
        if value:
            record = index.get(str(value))
            if record is not None:
                return record
    provenance = item.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    source_id = item.get("source_id") or provenance.get("source_id") or ""
    relative_path = (
        item.get("relative_path") or provenance.get("relative_path") or relative_path_of(node_id)
    )
    if source_id and relative_path:
        return index.get(f"{source_id}{_PATH_KEY}{relative_path}")
    return None


def describe(record: Mapping[str, Any]) -> dict[str, Any]:
    """What a search result says about the memory it came from."""
    return {
        "record_id": record.get("record_id"),
        "scope": record.get("scope"),
        "subject": record.get("subject"),
        "kind": record.get("kind"),
        "asserted_at": record.get("asserted_at"),
    }
