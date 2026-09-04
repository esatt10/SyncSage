"""One gate vocabulary, and an empty case that cannot be constructed.

`all([])` is `True`. That produced a real defect: a skipped evaluation run
carried no gates, `gates_passed` answered `True`, and `pheasant eval run`
turned it into a zero exit status — a green CI signal for a batch that never
ran. The evaluation plane fixed it with `bool(gates) and all(...)`. The tuning
plane, written afterwards, shipped its *own* copy of that guard, with a
docstring citing the evaluation plane's incident as the reason.

The lesson propagated; the code did not. A third plane needing gates would
start from zero with a one-in-two chance of writing `all(...)`, because
nothing made the empty case impossible.

So these tests pin the invariant where it now lives — as a constructor
precondition — and then check that both existing planes actually go through
it rather than keeping their own copies.
"""

from __future__ import annotations

import dataclasses

import pytest

from pheasant.decision import (
    EmptyGateSet,
    Gate,
    GateSet,
    all_passed,
    blocking_failures,
)

# ---------------------------------------------------------------------------
# The invariant, as a precondition
# ---------------------------------------------------------------------------


def test_a_gate_set_cannot_be_constructed_empty() -> None:
    """The whole point: not a guarded check, an unrepresentable state."""

    with pytest.raises(EmptyGateSet) as refused:
        GateSet([])
    assert "at least one gate" in str(refused.value)
    # And the message says what to do instead, because a caller that
    # legitimately has none needs somewhere to go.
    assert "GateSet.of()" in str(refused.value)


def test_no_gates_is_absence_rather_than_an_empty_set() -> None:
    """`None` has no `passed` to misread, which is the structural fix.

    An empty list has a `passed` you can compute; the answer is just wrong.
    Absence cannot be asked the question at all.
    """

    assert GateSet.of([]) is None
    assert GateSet.of(None) is None
    assert GateSet.of([Gate("g", True)]) is not None


def test_a_decision_against_no_gates_has_not_passed_them() -> None:
    assert all_passed([]) is False
    assert all_passed(None) is False
    assert all_passed([Gate("g", passed=True)]) is True
    assert all_passed([Gate("a", True), Gate("b", False)]) is False


def test_the_invariant_holds_for_both_payload_shapes() -> None:
    """The evaluation plane holds dataclasses, the tuning plane holds dicts.

    Unifying those would mean changing two persisted wire formats to no
    purpose; what had to be unified is the rule, so the rule reads both.
    """

    assert all_passed([{"gate_id": "x", "passed": True}]) is True
    assert all_passed([{"gate_id": "x", "passed": False}]) is False
    assert all_passed([{"gate_id": "x"}]) is False  # absent == not passed


def test_blocking_failures_names_only_the_gates_that_may_veto() -> None:
    gates = [
        Gate("hard", passed=False, blocking=True),
        Gate("soft", passed=False, blocking=False),
        Gate("fine", passed=True, blocking=True),
    ]
    assert blocking_failures(gates) == ["hard"]
    assert GateSet(gates).blocking_failures == ["hard"]
    # No gates at all yields no failures — and `all_passed` is what says that
    # is not a pass. The two questions are separate on purpose.
    assert blocking_failures([]) == []
    assert all_passed([]) is False


def test_a_gate_set_is_always_truthy() -> None:
    """Defined explicitly so a reader need not derive it from __len__, and so
    `if gate_set:` cannot accidentally mean `if gates:` did."""

    assert bool(GateSet([Gate("g", True)])) is True
    assert len(GateSet([Gate("a", True), Gate("b", True)])) == 2


def test_a_gate_is_frozen() -> None:
    """A decision record that can be edited after the fact is not a record."""

    gate = Gate("g", passed=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.passed = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Both planes go through it
# ---------------------------------------------------------------------------


def test_the_evaluation_plane_delegates() -> None:
    """A skipped run must not report that it passed gates it never evaluated."""

    from pheasant.evaluation.contracts import GateResult
    from pheasant.evaluation.runner import RunOutcome

    skipped = RunOutcome(run_id="r", snapshot_id="s", status="skipped")
    assert skipped.gates == []
    assert skipped.gates_passed is False

    ran = RunOutcome(
        run_id="r",
        snapshot_id="s",
        status="completed",
        gates=[GateResult(gate_id="acl", passed=True, observed=0.0, maximum=0.0)],
    )
    assert ran.gates_passed is True


def test_the_tuning_plane_delegates() -> None:
    from pheasant.tuning.contracts import Decision

    empty = Decision(decision_id="d", experiment_id="e", outcome="declined", reason="no")
    assert empty.gates_passed is False

    decided = Decision(
        decision_id="d",
        experiment_id="e",
        outcome="promoted",
        reason="yes",
        gates=({"gate_id": "holdout", "passed": True},),
    )
    assert decided.gates_passed is True


def test_the_tuning_gate_builder_keeps_its_wire_shape() -> None:
    """Routing through the shared type must not change what is persisted.

    Decision records and the UI already read these keys; a vocabulary
    consolidation that rewrote them would be a migration wearing a refactor's
    clothes.
    """

    from pheasant.tuning.gates import gate

    built = gate("holdout", True, summary="confirmed", observed=0.1, threshold=0.005)
    assert set(built) == {"gate_id", "passed", "blocking", "summary", "observed", "threshold"}
    assert built["gate_id"] == "holdout"
    assert built["passed"] is True
    assert built["blocking"] is True


def test_neither_plane_still_spells_the_guard_itself() -> None:
    """The duplication this removed, asserted gone.

    Without this the two copies can drift back in one careless edit — which is
    how there came to be two of them in the first place.
    """

    import re
    from pathlib import Path

    from pheasant import decision as decision_module

    root = Path(decision_module.__file__).resolve().parent
    offenders: list[str] = []
    # `bool(self.gates) and all(...)` and friends: the remembered check.
    pattern = re.compile(r"bool\(\s*(?:self\.)?gates\s*\)\s*and\s+all\(")
    for path in sorted(root.rglob("*.py")):
        if path.name == "decision.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert not offenders, (
        "the empty-gate guard is spelled by hand again in: "
        + ", ".join(offenders)
        + " — use pheasant.decision.all_passed, which cannot be vacuous."
    )
