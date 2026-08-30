"""The data contracts of the evaluation plane.

Everything here is a *record about* the knowledge base, never a piece of it.
That distinction is the first normative principle of the specification and the
one that decides where this package sits: an evaluation row is not a file, is
never chunked, never indexed, and never returned by an ordinary search -- the
same posture ``telemetry/interactions.py`` takes for observations, for the same
reason. A region that measures itself must not then retrieve its own
measurements as knowledge.

Four things in this module are load-bearing.

**Identity is derived, never minted.** A ``query_id`` is a digest of the
normalized query text, so the same question asked in two snapshots is the same
query in both. Without that, paired baseline/treatment comparison and frozen
anchor cohorts are impossible: you cannot subtract two runs whose rows do not
line up. Snapshot, cohort, variant and run ids are digests of their own content
for the same reason -- two replicas computing a manifest for one state produce
one id, which is what makes the fleet case work at all.

**Every metric carries its denominator and its evidence.** :class:`MetricResult`
has no shape in which a bare number can be published: numerator, denominator,
formula, the substituted calculation, the operands, the proof references, the
excluded count with reasons, and the one limitation are all required fields.
The specification states this as a MUST and it is enforced structurally rather
than by convention, because a score without its denominator is exactly the
artifact this whole plane exists to avoid producing.

**Unknown is a value, not a missing one.** :class:`Polarity` has three members
and ``status`` has ``insufficient_evidence`` and ``not_applicable`` alongside
pass/warn/fail. An unjudged artifact is not a negative one, and a metric that
could not be computed is not a metric that scored zero -- collapsing either is
how an evaluation starts lying.

**Classification travels with the number.** ``demonstrated`` (backed by proof),
``structural`` (deterministic system behavior), ``diagnostic`` (corpus-relative
geometry and consensus) and ``operational`` are different kinds of claim, and a
reader who cannot see which one they are holding will treat cosine proximity as
factual accuracy. The field is required on every result.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import blake2b
from typing import Any

#: Bumped when a field is removed, renamed or retyped -- never when one is
#: added, so a report written against version 1 keeps parsing as this grows.
#: Same contract the interaction ledger and the Parquet export both make.
EVALUATION_SCHEMA_VERSION = 1

#: Length of every derived id's digest. 16 hex characters is 64 bits: far past
#: collision risk for a corpus of queries, short enough to read in a log line.
_DIGEST_CHARS = 16

_WHITESPACE = re.compile(r"\s+")


def utc_now() -> str:
    """An instant, spelled the one way the whole plane spells it."""

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(*parts: Any) -> str:
    """A stable content digest over the given parts.

    ``json.dumps(..., sort_keys=True)`` for anything structured, so a mapping
    built in two different orders in two processes digests identically. That
    property is what makes a snapshot id computable on any replica.
    """

    hasher = blake2b(digest_size=_DIGEST_CHARS // 2)
    for part in parts:
        if isinstance(part, (dict, list, tuple)):
            encoded = json.dumps(part, sort_keys=True, separators=(",", ":"), default=str)
        else:
            encoded = "" if part is None else str(part)
        hasher.update(encoded.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def normalize_query(text: str) -> str:
    """The canonical spelling of a query for identity purposes.

    Case-folded and whitespace-collapsed, and nothing else. Deliberately not
    stemmed, stopword-stripped or reordered: those are *retrieval* decisions,
    and folding them into identity would merge two queries the region answers
    differently into one row that claims to be both.
    """

    return _WHITESPACE.sub(" ", (text or "").strip()).casefold()


def query_id(text: str) -> str:
    """``query-<digest>`` for a query's canonical text.

    Stable across snapshots by construction -- which is what lets an anchor
    cohort frozen months ago still name the same questions today.
    """

    return f"query-{digest(normalize_query(text))}"


class Polarity(StrEnum):
    """Whether a proof event supports, opposes, or says nothing.

    ``UNKNOWN`` is first-class and is the default for exposure. Non-selection
    is not evidence of irrelevance -- the reader may simply have found what
    they needed at rank one -- and a system that treats silence as a negative
    manufactures negatives at exactly the rate it serves results.
    """

    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


class Strength(StrEnum):
    """How much a proof event is worth being right about.

    Ordered, and the order matters: sufficiency rules and promotion gates ask
    for "at least strong", so the members carry a rank rather than relying on
    declaration order surviving a refactor.
    """

    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    CONCLUSIVE = "conclusive"

    @property
    def rank(self) -> int:
        return {"weak": 0, "moderate": 1, "strong": 2, "conclusive": 3}[self.value]


class Classification(StrEnum):
    """What kind of claim a metric is making. Required on every result."""

    DEMONSTRATED = "demonstrated"
    STRUCTURAL = "structural"
    DIAGNOSTIC = "diagnostic"
    OPERATIONAL = "operational"


class MetricStatus(StrEnum):
    """How to read a metric's value -- including "do not read it as a value".

    ``INSUFFICIENT_EVIDENCE`` and ``NOT_APPLICABLE`` exist so a metric that
    could not be computed never has to be reported as ``0.0``. The
    specification names that substitution explicitly as a failure mode, and it
    is the one that quietly turns a sparse cohort into an alarming dashboard.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFORMATIONAL = "informational"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_APPLICABLE = "not_applicable"


class CohortPurpose(StrEnum):
    """Why a set of queries is evaluated together.

    ``LEARNED`` and ``TEMPORAL_HOLDOUT`` are separate members rather than a
    flag because the reporting rule is that they may never be merged: recall
    of the experience that created a memory is not evidence that the memory
    generalizes, and the type system is the cheapest place to keep the two
    apart.
    """

    ANCHOR = "anchor"
    ROLLING = "rolling"
    LEARNED = "learned"
    TEMPORAL_HOLDOUT = "temporal_holdout"
    CONTROL = "control"
    SYNTHETIC = "synthetic"


class TargetType(StrEnum):
    ARTIFACT = "artifact"
    MEMORY = "memory"
    FACT = "fact"
    RESPONSE = "response"
    ACTION = "action"
    QUERY = "query"


@dataclass(frozen=True)
class SnapshotManifest:
    """One immutable, reproducible knowledge-base state.

    Every field is an input capable of affecting retrieval or evaluation. That
    completeness is the point: two runs are comparable only when the fields
    that differ between them can be enumerated, and a field nobody recorded
    cannot be enumerated. :meth:`differences` is what a comparison prints.
    """

    snapshot_id: str
    kb_id: str
    created_at: str
    effective_as_of: str
    corpus: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    #: Fields the builder could not resolve. A manifest that is *known* to be
    #: partial is honest; one that silently defaults a digest to "" and calls
    #: itself complete is what ``incomplete_snapshot_blocks_run`` guards.
    incomplete: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.incomplete

    def sections(self) -> dict[str, dict[str, Any]]:
        return {
            "corpus": self.corpus,
            "graph": self.graph,
            "retrieval": self.retrieval,
            "memory": self.memory,
            "security": self.security,
            "evaluation": self.evaluation,
        }

    def differences(self, other: SnapshotManifest) -> list[str]:
        """Dotted names of every manifest field that differs.

        A comparison MUST list these. "The corpus changed" is not a finding a
        reader can act on; ``corpus.content_manifest_digest`` is.
        """

        out: list[str] = []
        mine, theirs = self.sections(), other.sections()
        for name in sorted(set(mine) | set(theirs)):
            left, right = mine.get(name, {}), theirs.get(name, {})
            for key in sorted(set(left) | set(right)):
                if left.get(key) != right.get(key):
                    out.append(f"{name}.{key}")
        return out

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["incomplete"] = list(self.incomplete)
        payload["schema_version"] = EVALUATION_SCHEMA_VERSION
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SnapshotManifest:
        known = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__}
        known["incomplete"] = tuple(known.get("incomplete") or ())
        return cls(**known)


@dataclass(frozen=True)
class Proof:
    """One typed observation about one target.

    The event type is preserved verbatim even when several types map to the
    same polarity and weight, because a configuration change that re-weights
    "cited" must be re-computable over evidence already collected. Collapsing
    the type at write time makes every past weighting permanent.

    ``weight`` is the product of four reported multipliers rather than an
    opaque scalar -- see :func:`pheasant.evaluation.proof.weigh`. A reader who
    is shown only the product cannot tell a strong event decayed by age from a
    weak fresh one.
    """

    proof_id: str
    kb_id: str
    query_id: str
    target_type: str
    target_id: str
    event_type: str
    polarity: str
    strength: str
    weight: float
    observed_at: str
    interaction_id: str | None = None
    snapshot_id: str | None = None
    #: Opaque, stable partition token -- never a principal. Proof is scoped to
    #: the security partition of the interaction that produced it; aggregating
    #: across partitions is a documented policy decision, not a default.
    principal_partition: str | None = None
    position: int | None = None
    exposed: bool = True
    outcome_reference: str | None = None
    supersedes_proof_id: str | None = None
    reason_code: str = ""
    multipliers: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> Proof:
        data = dict(row)
        raw = data.pop("multipliers_json", None)
        try:
            multipliers = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            multipliers = {}
        data["multipliers"] = multipliers
        data["exposed"] = bool(data.get("exposed", 1))
        data["weight"] = float(data.get("weight") or 0.0)
        if data.get("position") is not None:
            data["position"] = int(data["position"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class EvaluatedQuery:
    """A query as a cohort holds it: identity, text, and when it was asked.

    Carries ``occurred_at`` because temporal replay needs it. A holdout cohort
    is defined by "asked after the intervention existed", and a query with no
    timestamp cannot be placed on either side of that line -- so one is
    excluded from holdout rather than assumed recent.
    """

    query_id: str
    text: str
    occurred_at: str | None = None
    principal_partition: str | None = None
    #: How many times the region was asked this. Usage weighting reads it.
    asked: int = 1
    #: Set on synthetic invariant cases: what the case asserts.
    expectation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Cohort:
    """A named set of queries evaluated together for a stated purpose."""

    cohort_id: str
    kb_id: str
    name: str
    purpose: str
    queries: tuple[EvaluatedQuery, ...] = ()
    created_at: str = ""
    frozen: bool = False
    window_start: str | None = None
    window_end: str | None = None
    eligibility_digest: str = ""

    @property
    def query_ids(self) -> tuple[str, ...]:
        return tuple(q.query_id for q in self.queries)

    @property
    def query_count(self) -> int:
        return len(self.queries)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["queries"] = [q.as_dict() for q in self.queries]
        payload["query_count"] = self.query_count
        return payload


@dataclass(frozen=True)
class Variant:
    """One retrieval configuration in the ablation matrix.

    A treatment differs from its baseline in *exactly* the intervention being
    measured; ``baseline_variant_id`` is what makes that pairing explicit
    rather than inferred from names. An ablation whose pair differs in two
    things measures neither.
    """

    variant_id: str
    label: str
    memory_results: str = "auto"
    steering_kinds: tuple[str, ...] = ()
    tiers: tuple[str, ...] | None = None
    baseline_variant_id: str | None = None
    candidate_ids: tuple[str, ...] = ()
    #: Records held out of this run entirely (leave-one-out attribution).
    excluded_record_ids: tuple[str, ...] = ()
    description: str = ""

    @property
    def configuration_digest(self) -> str:
        return digest(
            self.memory_results,
            sorted(self.steering_kinds),
            sorted(self.tiers or ()),
            sorted(self.candidate_ids),
            sorted(self.excluded_record_ids),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steering_kinds"] = list(self.steering_kinds)
        payload["tiers"] = list(self.tiers) if self.tiers is not None else None
        payload["candidate_ids"] = list(self.candidate_ids)
        payload["excluded_record_ids"] = list(self.excluded_record_ids)
        payload["retrieval_configuration_digest"] = self.configuration_digest
        return payload


@dataclass(frozen=True)
class MetricScope:
    """Which snapshot, cohort, variant and query a result belongs to.

    ``query_id`` is set on per-query results and ``None`` on aggregates, which
    is what lets one table hold both and an aggregate resolve to the rows that
    produced it -- the traceability acceptance criterion.
    """

    snapshot_id: str
    cohort_id: str | None = None
    variant_id: str | None = None
    query_id: str | None = None


@dataclass
class MetricResult:
    """One measurement, with everything a reader needs to argue with it.

    Constructing one of these without a formula, a substituted calculation, a
    denominator and a limitation is deliberately awkward: the specification
    requires all four on every published metric, and the cheapest enforcement
    is to make the omission visible at the call site rather than at review
    time. :meth:`validate` is the mechanical check the run applies before
    persisting anything.
    """

    metric_id: str
    classification: str
    scope: MetricScope
    value: float | None
    formula: str
    substituted: str
    numerator: float | None = None
    denominator: float | None = None
    unit: str = "proportion"
    status: str = MetricStatus.INFORMATIONAL.value
    metric_version: int = 1
    optional: bool = False
    operands: dict[str, Any] = field(default_factory=dict)
    proof_ids: tuple[str, ...] = ()
    interaction_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    excluded_count: int = 0
    exclusion_reasons: dict[str, int] = field(default_factory=dict)
    threshold: float | None = None
    summary: str = ""
    supports_claim: str = ""
    does_not_support: str = ""

    def validate(self) -> list[str]:
        """Reasons this result may not be published, or an empty list."""

        problems: list[str] = []
        if not self.formula:
            problems.append("missing formula")
        if not self.substituted:
            problems.append("missing substituted calculation")
        if not self.does_not_support:
            problems.append("missing limitation")
        # A value without a denominator is the artifact this plane exists to
        # avoid. Statuses that mean "there is no value" are exempt, because
        # there is then nothing to be misread.
        valueless = {
            MetricStatus.INSUFFICIENT_EVIDENCE.value,
            MetricStatus.NOT_APPLICABLE.value,
        }
        if self.value is not None and self.denominator is None and self.status not in valueless:
            problems.append("published value with no denominator")
        if self.classification not in set(Classification):
            problems.append(f"unknown classification: {self.classification}")
        if self.status not in set(MetricStatus):
            problems.append(f"unknown status: {self.status}")
        return problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "metric_version": self.metric_version,
            "classification": self.classification,
            "optional": self.optional,
            "scope": asdict(self.scope),
            "result": {
                "value": self.value,
                "numerator": self.numerator,
                "denominator": self.denominator,
                "unit": self.unit,
                "status": self.status,
                "threshold": self.threshold,
            },
            "calculation": {
                "formula": self.formula,
                "substituted": self.substituted,
                "operands": self.operands,
            },
            "evidence": {
                "proof_ids": list(self.proof_ids),
                "interaction_ids": list(self.interaction_ids),
                "artifact_ids": list(self.artifact_ids),
                "excluded_count": self.excluded_count,
                "exclusion_reasons": dict(self.exclusion_reasons),
            },
            "interpretation": {
                "summary": self.summary,
                "supports_claim": self.supports_claim,
                "does_not_support": self.does_not_support,
            },
        }


@dataclass
class GateResult:
    """A hard invariant, evaluated before any score is aggregated.

    Gates are not metrics with a low threshold. An ACL leak is not offset by
    good recall, and the arithmetic that would let it be is exactly what
    ``passed`` refuses to participate in.
    """

    gate_id: str
    passed: bool
    observed: float
    maximum: float
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
