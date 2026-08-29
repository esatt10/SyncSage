"""Turning recorded interactions into memory, deterministically.

The observation plane records what was asked, on which surface, by whom, and
what came back. This is the half that reads it -- and the boundary it must not
cross is the whole design: **an observation is evidence, a record is memory,
and only an admission crosses.** Admission goes through
:meth:`pheasant.memory.store.MemoryStore.append` like every other write, so
memory's first invariant ("records are files, no second ingestion path, no
direct index writes") never bends here.

**No model runs in this path.** Rules are counting and string matching over
recorded rows, so a pass is reproducible: the same ledger and the same
parameters produce byte-identical text, and therefore -- because a record's id
is a digest of ``scope|subject|text`` -- the same record id. That is what makes
a repeat pass free rather than merely idempotent-by-luck: an unchanged session
re-derives the record it already wrote and the store dedups it on the file.

## The session digest

The first rule, and the one plan 2 was written about: *a session has a single
memory, refined through dialog.* That needs no new primitive. It is a
supersession chain --- scope ``session``, subject the session id, each
refinement naming the previous one in ``supersedes`` --- so:

* ``current_only`` (on by default) returns **exactly one** record per session;
* ``as_of`` reads the session's history, which is the point of invalidating
  rather than overwriting;
* consolidation archives the chain on the ordinary beat;
* ``session_ttl_days`` decays it like any other session-scope memory.

**Why this is written automatically while everything else is proposed.** A
session digest is `scope: session`, subject that session, written by that
principal --- so under ``security.acl_enforced`` only its own writer can read
it, and it decays with the session. It never becomes shared knowledge: reaching
``user`` or ``org`` scope takes an explicit promotion, which is exactly the
"nothing persists into the knowledge base unless a person adds it" the design
asks for. What is automatic here is a session's memory of itself.

A digest is written only for a session with at least
``memory.formation.min_observations`` recorded interactions. One question is
not a dialog, and a record per drive-by query would be the unbounded growth the
capacity rules exist to prevent.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pheasant.memory.store import MemoryRecord, MemoryStore, memory_source

logger = logging.getLogger(__name__)

#: Versioned, and a new version is a **new id** rather than an edit to this
#: one: a record's provenance has to stay attributable after the logic that
#: produced it changes.
SESSION_DIGEST_RULE = "session-digest-v1"

#: The tag every formed record carries, so a machine-authored record is always
#: distinguishable from a written one -- the posture `llm-synthesized` already
#: establishes for synthesis.
FORMED_TAG = "formed"

#: How much of a session one digest states. Caps, not thresholds: a digest is
#: a paragraph a person reads in the Memory tab, and an unbounded one is a
#: transcript, which is the thing this deliberately is not.
MAX_QUERIES = 10
MAX_PATHS = 10
MAX_GAPS = 5

#: Below this score a hit is not evidence the region answered anything. The
#: same number `retrieval-gap-v1` will key off, kept here so the two cannot
#: disagree about what "answered" means.
ANSWERED_SCORE = 0.0


@dataclass
class SessionObservations:
    """One principal's interactions within one session.

    Keyed by ``(session_id, principal)`` rather than by session alone. A
    session id is caller-asserted, so two principals *can* claim one, and a
    digest that mixed them would be readable by whichever writer happened to
    own the record --- an ACL leak reached through a field nobody
    authenticates. Splitting them is the conservative reading and costs
    nothing when, as normally, a session has one principal.
    """

    session_id: str
    principal: str | None
    modalities: set[str] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)
    paths: Counter[str] = field(default_factory=Counter)
    gaps: list[str] = field(default_factory=list)
    observations: int = 0
    first_seen: str = ""
    last_seen: str = ""


def _candidate_sessions(state: Any, *, min_observations: int, limit: int) -> list[str]:
    """Session ids worth digesting, most recently active first.

    A separate, aggregate-only query so the pass is bounded *before* any row
    bodies are read. Selecting rows first and truncating them would cut a
    session in half, and a half-digest changes on the next pass and supersedes
    itself forever --- churn that looks like activity.

    Most recently active first because a backlog should drain from the live
    end: a session someone is still in is worth more than one that ended
    yesterday and will be identical whenever it is reached.
    """

    rows = state.rows(
        "SELECT session_id, MAX(started_at) AS last_seen FROM interaction_events "
        "WHERE session_id IS NOT NULL AND session_id <> '' "
        "GROUP BY session_id HAVING COUNT(*) >= ? "
        "ORDER BY MAX(started_at) DESC, session_id LIMIT ?",
        (int(min_observations), int(limit)),
    )
    return [str(row["session_id"]) for row in rows]


def _rows(state: Any, session_ids: list[str]) -> list[Any]:
    """Every ledger row for the named sessions, in dialog order.

    ``ORDER BY started_at`` is what makes the digest's "asked about" list the
    order the session actually asked in --- and deterministic, which the record
    id depends on.
    """

    if not session_ids:
        return []
    placeholders = ",".join("?" for _ in session_ids)
    return state.rows(
        "SELECT session_id, principal, modality, query_text, result_paths_json, "
        "result_count, top_score, started_at "
        f"FROM interaction_events WHERE session_id IN ({placeholders}) "
        "ORDER BY started_at, id",
        tuple(session_ids),
    )


def collect_sessions(
    state: Any, *, min_observations: int, max_sessions: int
) -> list[SessionObservations]:
    """Group ledger rows into per-(session, principal) evidence.

    Grouped in Python rather than SQL because the interesting parts --- the
    distinct queries in order, the path frequencies, which calls answered
    nothing --- are list-shaped, and expressing them as portable SQL across
    SQLite and Postgres would cost more than it saves on a hot window bounded
    to days.
    """

    candidates = _candidate_sessions(state, min_observations=min_observations, limit=max_sessions)
    grouped: dict[tuple[str, str | None], SessionObservations] = {}
    for row in _rows(state, candidates):
        session_id = str(row["session_id"])
        principal = row["principal"] or None
        key = (session_id, principal)
        entry = grouped.get(key)
        if entry is None:
            entry = SessionObservations(session_id=session_id, principal=principal)
            grouped[key] = entry
        entry.observations += 1
        entry.modalities.add(str(row["modality"] or ""))
        started = str(row["started_at"] or "")
        entry.first_seen = entry.first_seen or started
        entry.last_seen = max(entry.last_seen, started)

        query = (row["query_text"] or "").strip()
        if query and query not in entry.queries:
            entry.queries.append(query)
        try:
            for path in json.loads(row["result_paths_json"] or "[]"):
                entry.paths[str(path)] += 1
        except (TypeError, ValueError):
            pass
        # A question that returned nothing is the most useful thing a session
        # can record: it is what the region could not answer.
        answered = int(row["result_count"] or 0) > 0 and (row["top_score"] or 0) > ANSWERED_SCORE
        if query and not answered and query not in entry.gaps:
            entry.gaps.append(query)

    ready = [entry for entry in grouped.values() if entry.observations >= min_observations]
    # Most recently active first, then by id so a tie is not storage order.
    ready.sort(key=lambda item: (item.last_seen, item.session_id), reverse=True)
    return ready[:max_sessions]


def digest_text(session: SessionObservations) -> str:
    """The record's body: a fixed template over sorted inputs.

    Deterministic by construction --- every list is truncated after an explicit
    sort, never left in insertion or storage order --- because the record id is
    a digest of this string. Two passes over an unchanged session must produce
    the same bytes, or every beat would write a new record superseding the last
    and the chain would grow without bound.

    Written to be read by a person in the Memory tab, and by a model as
    *recorded assertion* rather than instruction, which is what the answering
    prompt already says of any remembered passage.
    """

    modality = "/".join(sorted(m for m in session.modalities if m)) or "unknown"
    lines = [
        f"Session {session.session_id} ({modality}): "
        f"{session.observations} interactions "
        f"from {session.first_seen or 'unknown'} to {session.last_seen or 'unknown'}."
    ]

    if session.queries:
        lines.append("")
        lines.append("Asked about:")
        # Insertion order is the order of the session's own dialog, which is
        # the meaningful order here and is already deterministic: the rows are
        # read `ORDER BY started_at`.
        lines.extend(f"- {query}" for query in session.queries[:MAX_QUERIES])

    if session.paths:
        lines.append("")
        lines.append("Most-consulted:")
        # Count descending, then path ascending: `Counter.most_common` alone
        # leaves ties in insertion order.
        ranked = sorted(session.paths.items(), key=lambda item: (-item[1], item[0]))
        lines.extend(f"- {path} ({count})" for path, count in ranked[:MAX_PATHS])

    if session.gaps:
        lines.append("")
        lines.append("Found nothing for:")
        lines.extend(f"- {gap}" for gap in session.gaps[:MAX_GAPS])

    return "\n".join(lines)


def _current_digest(
    records: list[MemoryRecord], session: SessionObservations
) -> MemoryRecord | None:
    """The session's live digest, if it already has one.

    Matched on ``(scope, subject, written_by)`` --- the same triple the record
    id is derived from --- so a second principal claiming the same session id
    gets its own chain rather than superseding somebody else's.
    """

    for record in records:
        if (
            record.scope == "session"
            and record.subject == session.session_id
            and (record.written_by or None) == session.principal
            and SESSION_DIGEST_RULE in (record.tags or ())
        ):
            return record
    return None


def run_session_digests(
    engine: Any,
    *,
    now: datetime | None = None,
    records: list[MemoryRecord] | None = None,
) -> dict[str, Any]:
    """Write or refine one digest per session. Returns a report.

    Refinement is supersession: when a session's evidence has changed, the new
    digest names the old one, so ``current_only`` still returns exactly one
    record per session and ``as_of`` can read what the session looked like an
    hour ago.

    When nothing changed, nothing is written. **Two independent guards make
    that true**, and it is worth saying which does what:

    * this short-circuit, which is a *cost* guard --- the common case is a
      store full of sessions nobody has touched since the last pass, and a
      fold still costs a file stat and a projection read per session;
    * the store's own dedup, which is the *correctness* one. A record's id is
      a digest of ``scope|subject|text`` (plus kind/writer), and ``supersedes``
      is deliberately **not** part of it --- so an unchanged digest re-derives
      the id it already wrote and the append collapses onto the existing file.

    Recorded because mutation testing says so: deleting the short-circuit
    leaves every test here green, and correctly so. It is not dead code, it is
    the cheap guard in front of the sound one.
    """

    config = engine.config
    settings = getattr(getattr(config, "memory", None), "formation", None)
    if settings is None or not getattr(settings, "enabled", False):
        return {}
    if not getattr(settings, "session_digest", True):
        return {}
    if SESSION_DIGEST_RULE not in (getattr(settings, "rules", None) or []):
        return {}
    source = memory_source(config, getattr(engine, "state", None))
    if source is None:
        return {}

    state = engine.state
    sessions = collect_sessions(
        state,
        min_observations=max(1, int(getattr(settings, "min_observations", 3))),
        max_sessions=max(1, int(getattr(settings, "max_candidates_per_pass", 50))),
    )
    if not sessions:
        return {}

    store = MemoryStore(source.path)
    live = records if records is not None else store.list_records(current_only=True)
    # `records` may be the whole store when a caller already parsed it; the
    # digest lookup needs current records only, and re-filtering here is
    # cheaper than a second glob.
    superseded = MemoryStore.superseded_ids(live)
    current = [record for record in live if record.record_id not in superseded]

    written: list[str] = []
    refined: list[str] = []
    unchanged = 0
    for session in sessions:
        text = digest_text(session)
        existing = _current_digest(current, session)
        if existing is not None and existing.text.strip() == text.strip():
            unchanged += 1
            continue
        try:
            record, created = store.append(
                text,
                scope="session",
                subject=session.session_id,
                supersedes=existing.record_id if existing is not None else None,
                tags=(SESSION_DIGEST_RULE, FORMED_TAG),
                written_by=session.principal,
                now=now,
            )
        except ValueError:
            # A session id that cannot be a subject (a newline, say) is caller
            # data on an unauthenticated surface. Skip it; never fail the beat.
            logger.debug("Skipped a session digest with an unusable subject", exc_info=True)
            continue
        if not created:
            unchanged += 1
            continue
        (refined if existing is not None else written).append(record.record_id)

    report: dict[str, Any] = {"rule_id": SESSION_DIGEST_RULE, "sessions": len(sessions)}
    if written:
        report["created"] = written
    if refined:
        report["refined"] = refined
    if unchanged:
        report["unchanged"] = unchanged
    return report
