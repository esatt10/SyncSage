"""The gate vocabulary, owned once.

A gate is a hard invariant evaluated *before* any score is aggregated: an ACL
leak is not offset by good recall, and the arithmetic that would let it be is
the thing gates refuse to participate in. Two planes need them — the
evaluation plane and the tuning plane — and a third eventually will.

This module exists because of one bug, and because the lesson from it
propagated while the code did not.

``all([])`` is ``True``. A skipped evaluation run carries no gates, so the
obvious spelling reported that a batch which never ran had passed every gate
it never evaluated — straight into ``pheasant eval run``'s exit status, which
is a green CI signal for a run that did not happen. The evaluation plane fixed
it with ``bool(self.gates) and all(...)``. The tuning plane, written later,
shipped *its own* copy of that guard, with a docstring recounting the
evaluation plane's incident as the justification.

Two implementations of one invariant, kept in agreement by nobody. A third
plane starting from scratch has a one-in-two chance of repeating the original
bug, because nothing in the codebase makes the empty case impossible.

So the invariant is a **constructor precondition** here rather than a
remembered check at each call site:

* :class:`GateSet` cannot be constructed empty — it raises.
* "No gates were evaluated" is expressed as the *absence* of a GateSet, and
  absence has no ``passed`` property to read.
* :func:`all_passed` is the one spelling for callers that hold a plain list,
  and it refuses vacuous truth by construction rather than by convention.

The payload shapes stay where they are. The evaluation plane's ``GateResult``
carries ``observed``/``maximum``/``evidence`` and is persisted into reports;
the tuning plane's gate dict carries ``observed``/``threshold``/``blocking``
and rides in a decision record. Unifying those would change two wire formats
to no purpose. What had to be unified is the *rule*, and the rule is here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, runtime_checkable


class EmptyGateSet(ValueError):
    """A gate set with nothing in it.

    Raised rather than tolerated: a caller that legitimately evaluated no
    gates has *no gate set*, which is a different thing from an empty one and
    is the distinction the original bug erased.
    """


@runtime_checkable
class GateLike(Protocol):
    """Anything that can report whether it passed."""

    @property
    def passed(self) -> bool: ...


class GateDict(TypedDict, total=False):
    """The dict rendering of a :class:`Gate`.

    Declared because this is what crosses the boundaries: it is persisted in
    decision records, returned over HTTP and MCP, and rendered by the UI. A
    renamed key here is a UI that shows a blank cell and a stored record that
    cannot be read back — neither of which raises anything.
    """

    gate_id: str
    passed: bool
    blocking: bool
    summary: str
    observed: Any
    threshold: Any
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Gate:
    """One gate result.

    Frozen because a gate is evidence: a decision record that could be edited
    after the fact is not a record. ``observed`` and ``threshold`` are carried
    separately from ``summary`` so a reader can argue with the number rather
    than with a sentence about it.
    """

    gate_id: str
    passed: bool
    summary: str = ""
    observed: Any = None
    threshold: Any = None
    #: Whether failing this gate may stop a promotion. A non-blocking gate is
    #: reported and does not decide — used for signals worth publishing that
    #: are not yet trusted enough to veto.
    blocking: bool = True
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> GateDict:
        payload: GateDict = {
            "gate_id": self.gate_id,
            "passed": self.passed,
            "blocking": self.blocking,
            "summary": self.summary,
            "observed": self.observed,
            "threshold": self.threshold,
        }
        if self.evidence:
            payload["evidence"] = dict(self.evidence)
        return payload


def _passed(gate: GateLike | Mapping[str, Any]) -> bool:
    """Read ``passed`` from either shape.

    Both planes are supported as they are — one holds dataclasses, the other
    dicts — because forcing a migration of two persisted formats is a much
    larger change than the invariant needs, and would be the reason not to do
    this at all.
    """

    if isinstance(gate, Mapping):
        return bool(gate.get("passed"))
    return bool(gate.passed)


def _blocking(gate: GateLike | Mapping[str, Any]) -> bool:
    if isinstance(gate, Mapping):
        return bool(gate.get("blocking", True))
    return bool(getattr(gate, "blocking", True))


def _gate_id(gate: GateLike | Mapping[str, Any]) -> str:
    if isinstance(gate, Mapping):
        return str(gate.get("gate_id", ""))
    return str(getattr(gate, "gate_id", ""))


class GateSet:
    """One or more gates, and the one place ``passed`` is decided.

    Constructing this with nothing raises :class:`EmptyGateSet`, which is what
    makes the vacuous case unrepresentable rather than merely guarded against.
    Callers that may have no gates use :meth:`of`, which returns ``None`` — and
    ``None`` has no ``passed`` to misread.
    """

    __slots__ = ("_gates",)

    def __init__(self, gates: Iterable[GateLike | Mapping[str, Any]]) -> None:
        items = tuple(gates)
        if not items:
            raise EmptyGateSet(
                "a GateSet needs at least one gate: a decision evaluated against no "
                "gates has not passed them, it has not been made. Use GateSet.of() if "
                "the caller may legitimately have none."
            )
        self._gates = items

    @classmethod
    def of(cls, gates: Iterable[GateLike | Mapping[str, Any]] | None) -> GateSet | None:
        """A gate set, or ``None`` when there is nothing to decide on."""

        items = tuple(gates or ())
        return cls(items) if items else None

    @property
    def passed(self) -> bool:
        """Every gate passed.

        Safe to spell as ``all(...)`` here, and only here, because the
        constructor has already excluded the input that makes ``all`` lie.
        """

        return all(_passed(gate) for gate in self._gates)

    @property
    def blocking_failures(self) -> list[str]:
        """Ids of the failed gates that are allowed to stop a promotion."""

        return [_gate_id(gate) for gate in self._gates if _blocking(gate) and not _passed(gate)]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            dict(gate)
            if isinstance(gate, Mapping)
            else (gate.as_dict() if hasattr(gate, "as_dict") else {"gate_id": _gate_id(gate)})
            for gate in self._gates
        ]

    def __iter__(self):
        return iter(self._gates)

    def __len__(self) -> int:
        return len(self._gates)

    def __bool__(self) -> bool:
        # Always true: a GateSet cannot be empty. Defined explicitly so a
        # reader does not have to derive that from __len__.
        return True


def all_passed(gates: Iterable[GateLike | Mapping[str, Any]] | None) -> bool:
    """Whether every gate passed — and never vacuously.

    The one spelling for a caller holding a plain list. ``False`` for an empty
    or absent list, because a decision evaluated against no gates has not
    passed them.
    """

    gate_set = GateSet.of(gates)
    return gate_set is not None and gate_set.passed


def blocking_failures(gates: Iterable[GateLike | Mapping[str, Any]] | None) -> list[str]:
    """Ids of failed blocking gates. Empty when there are no gates at all."""

    gate_set = GateSet.of(gates)
    return gate_set.blocking_failures if gate_set is not None else []
