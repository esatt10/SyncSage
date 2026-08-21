"""Phase 1 — L0 write-path admission: normalization, reinforcement, ACL.

Acceptance: a write whose *normalized* text matches a live record in the
same (scope, subject, kind, ACL-partition) bucket reinforces that record
(`created=False`, `outcome="reinforced"`, `observations` bumped) instead of
creating a new file — for an exact repeat exactly as for a paraphrase. Two
principals asserting the same sentence at `user`/`session` scope must never
collapse into one record (that would be a cross-principal read through the
write path). `pheasant.memory.normalize` must never fold text with real
argument structure (no token-sort, no stopword removal).
"""

from __future__ import annotations

from pathlib import Path

from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.memory.normalize import acl_class_for, normalized_digest, normalized_text
from pheasant.security.acl import normalize_acl


def _write_config(tmp_path: Path, *, reinforcement: bool | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(exist_ok=True)
    knob = (
        f"\n  reinforcement_enabled: {'true' if reinforcement else 'false'}"
        if reinforcement is not None
        else ""
    )
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: reinforcement-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:{knob}
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# normalize.py — the equivalence relation itself
# ---------------------------------------------------------------------------


def test_normalized_text_folds_case_whitespace_punctuation_and_framing():
    assert normalized_text("The rollout password is BANANAS.") == "rollout password is bananas"
    assert (
        normalized_text("  the ROLLOUT   password is bananas!!  ") == "rollout password is bananas"
    )
    assert (
        normalized_text("Note that the rollout password is bananas")
        == "rollout password is bananas"
    )
    assert normalized_text("FYI, the rollout password is bananas.") == "rollout password is bananas"


def test_normalized_text_never_token_sorts_or_drops_quantifiers():
    # Argument structure: reversing the operands must not compare equal.
    assert normalized_text("A supersedes B") != normalized_text("B supersedes A")
    # A quantifier changes what a fact means; it must survive normalization.
    assert normalized_text("The build is not passing") != normalized_text("The build is passing")
    assert "not" in normalized_text("The build is not passing")


def test_acl_class_mirrors_normalize_acl_partition():
    # org: one shared partition, matching normalize_acl's `public = True`.
    assert acl_class_for("org", "alice") == "*"
    assert acl_class_for("org", None) == "*"
    # user/session with a writer: one partition per writer, matching
    # normalize_acl's `allow.append(...)` branch.
    assert acl_class_for("user", "alice") == "alice"
    assert acl_class_for("session", "bob") == "bob"
    assert acl_class_for("user", "alice") != acl_class_for("user", "bob")
    # No recorded writer at user/session falls through to the region default
    # in normalize_acl (returns None); bucketed under "" here, which can only
    # ever match another equally-unattributed record.
    assert acl_class_for("user", None) == ""
    assert normalize_acl("memory", {"scope": "user", "written_by": ""}) is None


def test_normalized_digest_is_versioned_and_scoped():
    a = normalized_digest(scope="org", subject=None, kind="fact", acl_class="*", text="hello")
    b = normalized_digest(scope="user", subject=None, kind="fact", acl_class="*", text="hello")
    assert a != b  # scope is part of the key


# ---------------------------------------------------------------------------
# append() end to end, via memory_write
# ---------------------------------------------------------------------------


def test_paraphrase_reinforces_instead_of_creating(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write(
            "reinforcement-test", "The rollout password is BANANAS.", scope="org"
        )
        assert first["created"] is True
        assert first["outcome"] == "created"

        again = tools.memory_write(
            "reinforcement-test", "  the ROLLOUT password is bananas!!  ", scope="org"
        )
        assert again["created"] is False
        assert again["outcome"] == "reinforced"
        assert again["record"]["record_id"] == first["record"]["record_id"]
        # The caller wrote something byte-different from what is stored —
        # that must be surfaced, not silently discarded.
        assert again["submitted_text"] == "the ROLLOUT password is bananas!!"

        rows = tools.state.memory_salience_rows()
        row = next(r for r in rows if r["record_id"] == first["record"]["record_id"])
        assert row["observations"] == 1
        assert row["last_seen"]
    finally:
        tools.engine.close()


def test_exact_repeat_also_reinforces_not_just_paraphrase(tmp_path: Path) -> None:
    """Before Phase 1, `created=False` on an exact repeat captured no signal
    at all — the cheapest possible evidence of importance was thrown away.
    An exact repeat is a degenerate case of the same canonical-key match, so
    it must reinforce too, not merely dedupe silently."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write("reinforcement-test", "Deploys run on Fridays.", scope="org")
        second = tools.memory_write("reinforcement-test", "Deploys run on Fridays.", scope="org")
        third = tools.memory_write("reinforcement-test", "Deploys run on Fridays.", scope="org")
        assert [r["outcome"] for r in (first, second, third)] == [
            "created",
            "reinforced",
            "reinforced",
        ]
        rows = tools.state.memory_salience_rows()
        row = next(r for r in rows if r["record_id"] == first["record"]["record_id"])
        assert row["observations"] == 2
        # An exact repeat is byte-identical to the stored record already —
        # nothing new to surface.
        assert "submitted_text" not in third
    finally:
        tools.engine.close()


def test_reinforcement_survives_a_full_projection_rebuild(tmp_path: Path) -> None:
    """observations/last_seen/variants are earned, not derived — a full
    sync rebuilds memory_records wholesale and must carry them over exactly
    as it already does for salience/uses/last_used_at."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write("reinforcement-test", "Deploys run on Fridays.", scope="org")
        tools.memory_write("reinforcement-test", "deploys run on fridays", scope="org")
        tools.engine.sync_source("agent-memory", "full")
        rows = tools.state.memory_salience_rows()
        row = next(r for r in rows if r["record_id"] == first["record"]["record_id"])
        assert row["observations"] == 1
    finally:
        tools.engine.close()


def test_reinforcement_disabled_by_config_falls_back_to_pre_phase_1(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, reinforcement=False))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write(
            "reinforcement-test", "The rollout password is BANANAS.", scope="org"
        )
        paraphrase = tools.memory_write(
            "reinforcement-test", "the rollout password is bananas", scope="org"
        )
        # With reinforcement off, a paraphrase is a genuinely different
        # exact-digest write and creates its own record, exactly as before
        # Phase 1.
        assert paraphrase["created"] is True
        assert paraphrase["record"]["record_id"] != first["record"]["record_id"]

        exact_repeat = tools.memory_write(
            "reinforcement-test", "The rollout password is BANANAS.", scope="org"
        )
        assert exact_repeat["created"] is False
        assert exact_repeat["outcome"] == "duplicate"
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# ACL isolation — the most important test in this module
# ---------------------------------------------------------------------------


def test_two_principals_writing_identical_text_never_collapse(tmp_path: Path) -> None:
    """Bob writing Alice's exact sentence at `user` scope must get his own
    record, never Alice's — a shared record could only carry one writer's
    ACL, which would be a cross-principal read through the write path."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        alice = tools.memory_write(
            "reinforcement-test",
            "My favorite color is blue.",
            scope="user",
            principal="alice",
        )
        bob = tools.memory_write(
            "reinforcement-test",
            "My favorite color is blue.",
            scope="user",
            principal="bob",
        )
        assert alice["record"]["record_id"] != bob["record"]["record_id"]
        assert bob["created"] is True
        assert bob["outcome"] == "created"
        # Nothing about Bob's write carries Alice's record.
        assert bob["record"]["written_by"] == "bob"

        # Alice re-asserting the same sentence, even as a paraphrase, must
        # still reinforce her own record — same-principal is exactly the
        # case reinforcement exists for.
        alice_again = tools.memory_write(
            "reinforcement-test",
            "  My Favorite color is BLUE!  ",
            scope="user",
            principal="alice",
        )
        assert alice_again["record"]["record_id"] == alice["record"]["record_id"]
        assert alice_again["outcome"] == "reinforced"
    finally:
        tools.engine.close()


def test_org_scope_reinforces_across_different_writers(tmp_path: Path) -> None:
    """The inverse of the isolation case: `org` is a shared assertion by
    design (normalize_acl makes it public regardless of who wrote it), so
    two different principals asserting the same org fact SHOULD fold into
    one record — that is not a leak, it is the intended behavior."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write(
            "reinforcement-test",
            "The office closes at 6pm.",
            scope="org",
            principal="alice",
        )
        second = tools.memory_write(
            "reinforcement-test",
            "the office closes at 6pm",
            scope="org",
            principal="bob",
        )
        assert second["record"]["record_id"] == first["record"]["record_id"]
        assert second["outcome"] == "reinforced"
    finally:
        tools.engine.close()


def test_unattributed_user_scope_writes_only_match_each_other(tmp_path: Path) -> None:
    """No recorded writer at user scope falls through to the region default
    in normalize_acl (same as today, pre-Phase-1) — bucketing these under
    the empty acl_class does not create a new ACL exposure: an attributed
    write (has a writer) must never match one with none."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        attributed = tools.memory_write(
            "reinforcement-test", "The wifi password is changing.", scope="user", principal="alice"
        )
        unattributed = tools.memory_write(
            "reinforcement-test", "the wifi password is changing", scope="user"
        )
        assert unattributed["record"]["record_id"] != attributed["record"]["record_id"]
        assert unattributed["outcome"] == "created"

        unattributed_again = tools.memory_write(
            "reinforcement-test", "THE WIFI PASSWORD IS CHANGING", scope="user"
        )
        assert unattributed_again["record"]["record_id"] == unattributed["record"]["record_id"]
        assert unattributed_again["outcome"] == "reinforced"
    finally:
        tools.engine.close()


def test_reinforcement_never_crosses_kind(tmp_path: Path) -> None:
    """A `fact` and a steering `alias` carrying the same normalized text are
    different assertions (one is recallable content, the other is
    retrieval machinery) and must never fold into one record."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        fact = tools.memory_write("reinforcement-test", "flock means pheasant-flock", scope="org")
        alias = tools.memory_write(
            "reinforcement-test", "flock means pheasant-flock", scope="org", kind="alias"
        )
        assert fact["record"]["record_id"] != alias["record"]["record_id"]
        assert alias["outcome"] == "created"
    finally:
        tools.engine.close()


def test_http_memory_write_reinforces_like_mcp(tmp_path: Path) -> None:
    """`POST /memory` must carry the same outcome/observations behavior as
    the MCP `memory_write` tool — the two protocols share one `MemoryPolicy`
    story already (see `docs/memory-system.md`), and this is part of it."""
    import pytest as _pytest

    fastapi_testclient = _pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        first = client.post(
            "/memory",
            json={"text": "Deploy freezes start Friday 5pm.", "scope": "org", "sync": True},
        )
        assert first.status_code == 200, first.text
        assert first.json()["outcome"] == "created"
        first_id = first.json()["record"]["record_id"]

        again = client.post(
            "/memory",
            json={"text": "deploy freezes start friday 5pm", "scope": "org", "sync": True},
        )
        assert again.status_code == 200, again.text
        body = again.json()
        assert body["created"] is False
        assert body["outcome"] == "reinforced"
        assert body["record"]["record_id"] == first_id
        assert body["submitted_text"] == "deploy freezes start friday 5pm"
    finally:
        app.state.engine.close()
