"""Forming memory from observed interactions.

The boundary this rests on: **an observation is evidence, a record is memory,
and only an admission crosses.** These tests pin the crossing --- that it goes
through the ordinary write path, that it is deterministic enough for a repeat
pass to write nothing, and that a session's digest can never be read by a
principal who did not produce it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.config.schema import PheasantConfig
from pheasant.memory.formation import (
    MAX_PATHS,
    MAX_QUERIES,
    SESSION_DIGEST_RULE,
    SessionObservations,
    collect_sessions,
    digest_text,
    run_session_digests,
)
from pheasant.memory.maintenance import run_memory_maintenance
from pheasant.memory.store import MemoryStore, memory_source
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent


def _config(tmp_path: Path, **formation: Any) -> tuple[PheasantConfig, Path]:
    docs = tmp_path / "ws" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe filewatch daemon restarts nightly at 0300 UTC.\n",
        encoding="utf-8",
    )
    for name in ("state", "exports", "memory"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    raw = {
        "pheasant": {
            "name": "kb",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path / "ws"),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        # Off: these tests put rows in the ledger directly, because what is
        # under test is what formation makes of them -- the surfaces that
        # produce them are covered in `test_interactions.py`. Leaving it on
        # would build a buffer this app never tears down (no TestClient, so no
        # lifespan) and leak the process-wide slot into whatever runs next.
        "observability": {"interactions": {"enabled": False}},
        "memory": {"formation": {"enabled": True, "min_observations": 3, **formation}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(docs),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
            {
                "name": "agent-memory",
                "type": "memory",
                "path": str(tmp_path / "memory"),
                "sync": {"on_startup": False},
            },
        ],
    }
    path = tmp_path / "pheasant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PheasantConfig.model_validate(raw), path


def _engine(tmp_path: Path, **formation: Any) -> Any:
    from pheasant.api.app import create_app

    config, path = _config(tmp_path, **formation)
    app = create_app(config, config_path=str(path))
    return app.state.engine


def _observe(
    engine: Any,
    session: str,
    queries: list[str],
    *,
    principal: str | None = "user:ada",
    paths: list[str] | None = None,
    answered: bool = True,
    modality: str = "ui",
    start: int = 0,
) -> None:
    """Put interactions in the ledger without going through HTTP.

    The surfaces that produce these are covered in `test_interactions.py`; what
    is under test here is what formation makes of them.
    """

    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality=modality,
                principal=principal,
                session_id=session,
                trace_id=f"{start + index:032x}",
                span_id=f"{start + index:016x}",
                started_at=f"2026-01-01T00:00:{start + index:02d}.000000Z",
                status="ok",
                query_text=query,
                result_paths=list(paths or ["runbook.md"]) if answered else [],
                result_count=1 if answered else 0,
                top_score=0.8 if answered else None,
            )
            for index, query in enumerate(queries)
        ],
    )


# --------------------------------------------------------------------------
# Off unless asked for
# --------------------------------------------------------------------------


def test_formation_is_off_by_default() -> None:
    settings = PheasantConfig().memory.formation

    assert settings.enabled is False
    assert settings.auto_admit is False
    # `session_digest` is True but only meaningful when `enabled` is, so a
    # default install forms nothing at all.
    assert settings.session_digest is True


def test_nothing_is_formed_while_formation_is_disabled(tmp_path: Path) -> None:
    engine = _engine(tmp_path, enabled=False)
    _observe(engine, "sess-a", ["one", "two", "three"])

    assert run_session_digests(engine) == {}

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert store.list_records() == []


def test_the_digest_rule_can_be_switched_off_on_its_own(tmp_path: Path) -> None:
    engine = _engine(tmp_path, session_digest=False)
    _observe(engine, "sess-a", ["one", "two", "three"])

    assert run_session_digests(engine) == {}


def test_a_rule_absent_from_the_configured_list_does_not_run(tmp_path: Path) -> None:
    """`rules` is the list an operator edits; a rule missing from it is off
    even when the feature is on."""

    engine = _engine(tmp_path, rules=["retrieval-gap-v1"])
    _observe(engine, "sess-a", ["one", "two", "three"])

    assert run_session_digests(engine) == {}


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def test_a_session_below_the_threshold_is_not_worth_a_record(tmp_path: Path) -> None:
    """One question is not a dialog, and a record per drive-by query is the
    unbounded growth the capacity rules exist to prevent."""

    engine = _engine(tmp_path, min_observations=3)
    _observe(engine, "sess-small", ["only one", "and two"])

    assert run_session_digests(engine) == {}


def test_a_session_at_the_threshold_gets_one(tmp_path: Path) -> None:
    engine = _engine(tmp_path, min_observations=3)
    _observe(engine, "sess-a", ["one", "two", "three"])

    report = run_session_digests(engine)

    assert report["rule_id"] == SESSION_DIGEST_RULE
    assert len(report["created"]) == 1


def test_a_pass_is_bounded(tmp_path: Path) -> None:
    """A first pass over a busy ledger must not write unboundedly; what it
    does not reach waits for the next beat."""

    engine = _engine(tmp_path, max_candidates_per_pass=2)
    for index in range(5):
        _observe(engine, f"sess-{index}", ["a", "b", "c"], start=index * 10)

    assert len(run_session_digests(engine)["created"]) == 2


# --------------------------------------------------------------------------
# One record per session, refined through dialog
# --------------------------------------------------------------------------


def test_a_refined_digest_supersedes_its_own_previous_version(tmp_path: Path) -> None:
    """ "A session has a single memory, refined through dialog" needs no new
    primitive: it is a supersession chain, so `current_only` returns exactly
    one record while `as_of` can still read what the session looked like
    earlier."""

    engine = _engine(tmp_path)
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    _observe(engine, "sess-a", ["one", "two", "three"])
    first = run_session_digests(engine)["created"][0]

    _observe(engine, "sess-a", ["four"], start=50)
    second = run_session_digests(engine)["refined"][0]

    assert second != first
    current = store.list_records(current_only=True)
    assert len(current) == 1, "a session must have exactly one live memory"
    assert current[0].record_id == second
    assert current[0].supersedes == first
    # Nothing is destroyed: the earlier version is still on disk and still
    # reachable through `as_of`.
    assert len(store.list_records()) == 2


def test_a_repeat_pass_over_unchanged_evidence_writes_nothing(tmp_path: Path) -> None:
    """The determinism property, from the outside. Without it every beat would
    write a record superseding the last, and the chain would grow forever.

    Two guards hold this up and the assertion below is deliberately on the
    outcome rather than on either of them: the pass short-circuits on
    unchanged text (cheap), and the store dedups an id it has already written
    (sound). Deleting the short-circuit alone must not change what ends up on
    disk -- which is the property this asserts.
    """

    engine = _engine(tmp_path)
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    _observe(engine, "sess-a", ["one", "two", "three"])
    run_session_digests(engine)
    before = [record.record_id for record in store.list_records()]

    second = run_session_digests(engine)

    assert second.get("created") is None
    assert second.get("refined") is None
    assert second["unchanged"] == 1
    # The chain did not grow, and the live record is the same file.
    assert [record.record_id for record in store.list_records()] == before
    assert len(store.list_records(current_only=True)) == 1


def test_the_same_evidence_produces_byte_identical_text() -> None:
    """A record's id is a digest of `scope|subject|text`, so the text has to be
    a pure function of the evidence -- every list truncated after an explicit
    sort, never left in storage order."""

    def build() -> SessionObservations:
        session = SessionObservations(
            session_id="s",
            principal="user:ada",
            modalities={"mcp", "ui"},
            queries=["b", "a"],
            observations=3,
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T00:01:00Z",
        )
        # Equal counts: `Counter.most_common` alone would leave these in
        # insertion order, which is not stable across two collections.
        session.paths.update(["z.md", "a.md", "z.md", "a.md"])
        return session

    assert digest_text(build()) == digest_text(build())
    text = digest_text(build())
    # Ties break on the path, so `a.md` precedes `z.md` at the same count.
    assert text.index("a.md") < text.index("z.md")
    # Queries stay in the order the session asked them, which is dialog order.
    assert text.index("- b") < text.index("- a")


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_two_principals_claiming_one_session_get_separate_memories(
    tmp_path: Path,
) -> None:
    """A session id is caller-asserted, so two principals *can* claim one. A
    digest that mixed them would be readable by whichever writer owned the
    record -- an ACL leak reached through a field nobody authenticates."""

    engine = _engine(tmp_path)
    _observe(engine, "shared", ["a", "b", "c"], principal="user:ada")
    _observe(engine, "shared", ["x", "y", "z"], principal="user:bo", start=20)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    records = store.list_records(current_only=True)
    assert len(records) == 2
    assert {record.written_by for record in records} == {"user:ada", "user:bo"}
    ada = next(r for r in records if r.written_by == "user:ada")
    assert "x" not in ada.text and "y" not in ada.text


def test_a_digest_is_scoped_to_its_session_and_its_writer(tmp_path: Path) -> None:
    """Which is what makes writing it automatically safe: it never becomes
    shared knowledge. Reaching user or org scope takes an explicit promotion."""

    from pheasant.security.acl import normalize_acl

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two", "three"], principal="user:ada")
    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    record = store.list_records(current_only=True)[0]
    assert record.scope == "session"
    assert record.subject == "sess-a"
    assert record.written_by == "user:ada"

    acl = normalize_acl("memory", {"scope": record.scope, "written_by": record.written_by})
    assert acl == {"allow": ["user:ada"], "public": False}


def test_a_formed_record_says_it_was_formed(tmp_path: Path) -> None:
    """A machine-authored record must always be distinguishable from a written
    one -- the posture `llm-synthesized` already establishes for synthesis."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two", "three"])
    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    tags = store.list_records(current_only=True)[0].tags
    assert SESSION_DIGEST_RULE in tags
    assert "formed" in tags


# --------------------------------------------------------------------------
# What the digest says
# --------------------------------------------------------------------------


def test_a_question_that_found_nothing_is_recorded_as_a_gap(tmp_path: Path) -> None:
    """The honest form of "usage expands the knowledge": usage cannot conjure
    facts the corpus lacks, but it can say which questions keep going
    unanswered."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["answered one", "answered two"])
    _observe(engine, "sess-a", ["nobody knows this"], answered=False, start=30)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    text = store.list_records(current_only=True)[0].text
    assert "Found nothing for:" in text
    assert "- nobody knows this" in text


def test_the_digest_is_bounded_however_long_the_session(tmp_path: Path) -> None:
    """A digest is a paragraph someone reads in the Memory tab. An unbounded
    one is a transcript, which is the thing this deliberately is not."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-long", [f"question {index}" for index in range(40)])

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    text = store.list_records(current_only=True)[0].text
    assert text.count("\n- ") <= MAX_QUERIES + MAX_PATHS + 5
    # It still reports the true total, so the record does not understate the
    # session it describes.
    assert "40 interactions" in text


def test_redaction_leaves_a_digest_that_still_says_something(tmp_path: Path) -> None:
    """`redact_text` drops what anyone typed. The structural half survives, so
    a region can keep learning its own shape without keeping its content."""

    engine = _engine(tmp_path)
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id="sess-r",
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                query_text=None,  # redacted at write time
                result_paths=["runbook.md"],
                result_count=1,
                top_score=0.8,
            )
            for index in range(3)
        ],
    )

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    text = store.list_records(current_only=True)[0].text
    assert "Asked about:" not in text
    assert "Most-consulted:" in text
    assert "runbook.md" in text


def test_the_modality_a_session_used_is_recorded(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two"], modality="mcp")
    _observe(engine, "sess-a", ["three"], modality="ui", start=20)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert "(mcp/ui)" in store.list_records(current_only=True)[0].text


# --------------------------------------------------------------------------
# On the beat, and reachable afterwards
# --------------------------------------------------------------------------


def test_formation_rides_the_maintenance_beat(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two", "three"])

    result = run_memory_maintenance(engine, now=datetime.now(UTC))

    assert result is not None
    assert len(result["formation"]["created"]) == 1


def test_the_beat_forms_nothing_when_formation_is_off(tmp_path: Path) -> None:
    """Rule 7's shape: a region that has not asked for this behaves exactly as
    it did before formation existed."""

    engine = _engine(tmp_path, enabled=False)
    _observe(engine, "sess-a", ["one", "two", "three"])

    result = run_memory_maintenance(engine, now=datetime.now(UTC))

    assert result is not None
    assert "formation" not in result


def test_a_formed_digest_is_an_ordinary_indexed_record(tmp_path: Path) -> None:
    """The crossing, end to end: a formed record goes through the same write
    path, gets indexed by the same pipeline, and is found by the same search as
    anything a caller wrote. No second ingestion path."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["filewatch daemon nightly", "two", "three"])
    run_session_digests(engine)

    engine.sync_source("agent-memory", "full")
    rows = engine.state.rows(
        "SELECT scope, subject, kind FROM memory_records WHERE scope='session'", ()
    )
    assert len(rows) == 1
    assert rows[0]["subject"] == "sess-a"
    # A fact, not a steering rule: it is knowledge about a session, not syntax
    # that re-ranks queries.
    assert rows[0]["kind"] == "fact"


def test_an_unusable_session_id_is_skipped_never_raised(tmp_path: Path) -> None:
    """Session ids are caller data on an unauthenticated surface. A newline in
    one would forge frontmatter, which the store rejects -- and the beat must
    survive that rather than dying on one bad row."""

    engine = _engine(tmp_path)
    _observe(engine, "bad\nid", ["one", "two", "three"])
    _observe(engine, "good-id", ["one", "two", "three"], start=20)

    report = run_session_digests(engine)

    assert len(report.get("created") or []) == 1
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert store.list_records(current_only=True)[0].subject == "good-id"


def test_collect_ignores_rows_with_no_session(tmp_path: Path) -> None:
    """Most traffic carries no session id at all, and a digest of "everything
    anonymous" would be one enormous record that changes on every request."""

    engine = _engine(tmp_path)
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                session_id=None,
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                query_text="anonymous",
            )
            for index in range(5)
        ],
    )

    assert collect_sessions(engine.state, min_observations=1, max_sessions=10) == []


@pytest.mark.parametrize("bad", [None, ""])
def test_a_session_with_no_principal_still_forms(tmp_path: Path, bad: str | None) -> None:
    """Unattributed memory falls through to the region default rather than
    inventing an owner -- the rule `normalize_acl` already follows."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-anon", ["one", "two", "three"], principal=bad)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert store.list_records(current_only=True)[0].written_by is None
