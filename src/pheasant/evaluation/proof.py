"""Typed evidence: what was demonstrated, what was refuted, and what nobody knows.

The taxonomy below is the whole argument of this package compressed into a
table. Three rows carry most of it:

* **Served is unknown.** An artifact appearing in a result list is exposure, not
  success. A system that scores itself on what it chose to show has measured its
  own confidence.
* **Not selected is unknown.** The reader may have found the answer at rank one
  and stopped. Treating silence as a negative manufactures negatives at exactly
  the rate the region serves results, and the resulting "precision" improves
  whenever the region returns *less*.
* **Superseded is negative for current-time use only.** A corrected fact was not
  false when it was written, and marking it false retroactively destroys the
  point-in-time recall (`as_of`) the memory system deliberately preserves.

Everything else follows from keeping those three honest.

**Weight is a product of four reported multipliers**, never an opaque scalar.
A reader shown only ``0.25`` cannot tell a conclusive outcome decayed by a year
from a fresh citation, and the two support very different claims. :func:`weigh`
returns both the product and its factors, and the metric result carries the
factors into the report.

**Positive and negative evidence never cancel silently.** :func:`aggregate`
reports ``P``, ``N`` and ``Net`` separately, and a target carrying both above a
floor is *conflicted* -- which is a distinct state from unknown and from
agreed, and the one a reviewer most needs to see.

**Event type survives weighting.** The stored row keeps ``event_type`` verbatim
even when several types collapse to one polarity and weight, because
re-weighting the taxonomy must be recomputable over evidence already collected.
The interaction ledger makes the same promise about ``modality`` and for the
same reason.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from pheasant.evaluation.contracts import (
    Polarity,
    Proof,
    Strength,
    TargetType,
    digest,
    utc_now,
)
from pheasant.evaluation.contracts import (
    query_id as make_query_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventKind:
    """One row of the evidence taxonomy."""

    event_type: str
    polarity: str
    strength: str
    default_weight: float
    note: str


#: The default taxonomy. Configurable -- an operator may re-weight or re-polarize
#: any row -- but the *names* are stable, because a stored proof references one
#: and a renamed row orphans every past observation of it.
DEFAULT_TAXONOMY: dict[str, EventKind] = {
    kind.event_type: kind
    for kind in (
        EventKind("considered", Polarity.UNKNOWN, Strength.WEAK, 0.0, "seen before truncation"),
        EventKind("served", Polarity.UNKNOWN, Strength.WEAK, 0.0, "exposure, not success"),
        EventKind(
            "included_in_context",
            Polarity.UNKNOWN,
            Strength.MODERATE,
            0.0,
            "an agent read it; that it helped is a separate claim",
        ),
        EventKind("cited", Polarity.POSITIVE, Strength.WEAK, 0.25, "evidence of use, not of truth"),
        EventKind("selected", Polarity.POSITIVE, Strength.MODERATE, 0.5, "observed utility"),
        EventKind(
            "explicit_accept", Polarity.POSITIVE, Strength.STRONG, 1.0, "demonstrated utility"
        ),
        EventKind(
            "downstream_success",
            Polarity.POSITIVE,
            Strength.STRONG,
            1.0,
            "the task the query served completed",
        ),
        EventKind(
            "deterministic_validation_pass",
            Polarity.POSITIVE,
            Strength.CONCLUSIVE,
            1.0,
            "a test or schema check the region does not get to argue with",
        ),
        EventKind(
            "explicit_reject", Polarity.NEGATIVE, Strength.STRONG, -1.0, "demonstrated disutility"
        ),
        EventKind(
            "downstream_failure", Polarity.NEGATIVE, Strength.STRONG, -1.0, "the task failed"
        ),
        EventKind(
            "deterministic_validation_fail",
            Polarity.NEGATIVE,
            Strength.CONCLUSIVE,
            -1.0,
            "verified failure",
        ),
        EventKind(
            "explicit_correction",
            Polarity.NEGATIVE,
            Strength.STRONG,
            -1.0,
            "negative for the corrected claim, at current time",
        ),
        EventKind(
            "superseded",
            Polarity.NEGATIVE,
            Strength.STRONG,
            -1.0,
            "temporal invalidity only; still true under as_of",
        ),
        EventKind(
            "immediate_reformulation",
            Polarity.UNKNOWN,
            Strength.WEAK,
            0.0,
            "friction indicator; the reader may simply have thought of a better question",
        ),
        EventKind(
            "not_selected",
            Polarity.UNKNOWN,
            Strength.WEAK,
            0.0,
            "no default relevance inference -- see the module docstring",
        ),
    )
}

#: Strength multipliers. ``strong`` and ``conclusive`` share 1.0 on purpose:
#: they differ in what they *license* (only conclusive evidence satisfies a
#: promotion gate that asks for it), not in how much they weigh.
DEFAULT_STRENGTH_MULTIPLIERS: dict[str, float] = {
    Strength.WEAK.value: 0.25,
    Strength.MODERATE.value: 0.5,
    Strength.STRONG.value: 1.0,
    Strength.CONCLUSIVE.value: 1.0,
}

#: Below this absolute net weight a target is neither positive nor negative.
#: Not zero: a single weak citation is not a "known positive", and a metric
#: named `known_positive_recall` that counts one would be over-claiming in its
#: own name.
DEFAULT_POSITIVE_FLOOR = 0.2


@dataclass(frozen=True)
class ProofPolicy:
    """The versioned rules that turn events into weights.

    Frozen and digestible: the digest goes in the snapshot manifest, so a
    metric that moved because the *weights* changed is distinguishable from one
    that moved because the region did.
    """

    event_weights: dict[str, float] = field(default_factory=dict)
    strength_multipliers: dict[str, float] = field(default_factory=dict)
    unknown_is_negative: bool = False
    non_selection_is_negative: bool = False
    temporal_decay_enabled: bool = False
    temporal_half_life_days: float = 180.0
    positive_floor: float = DEFAULT_POSITIVE_FLOOR
    #: Sufficiency conditions. A metric computed over less than these is
    #: published with status ``insufficient_evidence`` rather than as a number
    #: nobody should act on.
    minimum_eligible_queries: int = 10
    minimum_evidenced_queries: int = 5
    minimum_independent_interactions: int = 5
    maximum_single_query_proof_share: float = 0.5

    @property
    def policy_digest(self) -> str:
        return digest(
            sorted(self.event_weights.items()),
            sorted(self.strength_multipliers.items()),
            self.unknown_is_negative,
            self.non_selection_is_negative,
            self.temporal_decay_enabled,
            self.temporal_half_life_days,
            self.positive_floor,
        )

    def weight_of(self, event_type: str) -> float:
        kind = DEFAULT_TAXONOMY.get(event_type)
        default = kind.default_weight if kind else 0.0
        return float(self.event_weights.get(event_type, default))

    def strength_of(self, event_type: str) -> str:
        kind = DEFAULT_TAXONOMY.get(event_type)
        # `str(...)` rather than the enum member: these values are written to a
        # TEXT column and returned in JSON, and a `StrEnum` member serializes
        # as `Strength.STRONG` through `repr` while comparing equal to
        # `"strong"`. Two spellings of one value in a stored row is how a
        # reader written against the API starts failing on the database.
        return str(kind.strength) if kind else Strength.WEAK.value

    def polarity_of(self, event_type: str) -> str:
        kind = DEFAULT_TAXONOMY.get(event_type)
        if kind is None:
            return Polarity.UNKNOWN.value
        if event_type == "not_selected" and self.non_selection_is_negative:
            return Polarity.NEGATIVE.value
        return str(kind.polarity)

    @classmethod
    def from_config(cls, settings: Any) -> ProofPolicy:
        """Build from ``evaluation.proof``, falling back to the defaults."""

        if settings is None:
            return cls(
                event_weights={k: v.default_weight for k, v in DEFAULT_TAXONOMY.items()},
                strength_multipliers=dict(DEFAULT_STRENGTH_MULTIPLIERS),
            )
        weights = {k: v.default_weight for k, v in DEFAULT_TAXONOMY.items()}
        weights.update(dict(getattr(settings, "event_weights", None) or {}))
        multipliers = dict(DEFAULT_STRENGTH_MULTIPLIERS)
        multipliers.update(dict(getattr(settings, "strength_multipliers", None) or {}))
        return cls(
            event_weights=weights,
            strength_multipliers=multipliers,
            unknown_is_negative=bool(getattr(settings, "unknown_is_negative", False)),
            non_selection_is_negative=bool(getattr(settings, "non_selection_is_negative", False)),
            temporal_decay_enabled=bool(getattr(settings, "temporal_decay_enabled", False)),
            temporal_half_life_days=float(getattr(settings, "temporal_half_life_days", 180.0)),
            positive_floor=float(getattr(settings, "positive_floor", DEFAULT_POSITIVE_FLOOR)),
            minimum_eligible_queries=int(getattr(settings, "minimum_eligible_queries", 10)),
            minimum_evidenced_queries=int(getattr(settings, "minimum_evidenced_queries", 5)),
            minimum_independent_interactions=int(
                getattr(settings, "minimum_independent_interactions", 5)
            ),
            maximum_single_query_proof_share=float(
                getattr(settings, "maximum_single_query_proof_share", 0.5)
            ),
        )


def _days_between(later: str, earlier: str) -> float:
    from datetime import datetime

    def parse(value: str) -> Any:
        text = (value or "").strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    left, right = parse(later), parse(earlier)
    if left is None or right is None:
        return 0.0
    return max(0.0, (left - right).total_seconds() / 86400.0)


def weigh(
    policy: ProofPolicy,
    event_type: str,
    *,
    strength: str | None = None,
    observed_at: str | None = None,
    now: str | None = None,
    source_reliability: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """``(weight, multipliers)`` for one event.

    The multipliers are returned rather than folded away because the
    specification requires every one of them to be reportable. Decay is
    *opt-in*: an operator who has not chosen a half-life should not discover
    that their year-old conclusive test result quietly stopped counting.
    """

    w_type = policy.weight_of(event_type)
    strength = strength or policy.strength_of(event_type)
    w_strength = float(policy.strength_multipliers.get(strength, 1.0))
    w_temporal = 1.0
    if policy.temporal_decay_enabled and observed_at:
        age = _days_between(now or utc_now(), observed_at)
        half_life = max(1e-6, float(policy.temporal_half_life_days))
        w_temporal = float(0.5 ** (age / half_life))
    w_source = float(source_reliability)
    multipliers = {
        "type": w_type,
        "strength": w_strength,
        "temporal": round(w_temporal, 6),
        "source": w_source,
    }
    return round(w_type * w_strength * w_temporal * w_source, 6), multipliers


def make_proof(
    *,
    kb_id: str,
    query_text: str | None = None,
    query_id: str | None = None,
    target_type: str,
    target_id: str,
    event_type: str,
    policy: ProofPolicy,
    observed_at: str | None = None,
    interaction_id: str | None = None,
    snapshot_id: str | None = None,
    principal_partition: str | None = None,
    position: int | None = None,
    exposed: bool = True,
    outcome_reference: str | None = None,
    reason_code: str = "",
    source_reliability: float = 1.0,
) -> Proof:
    """One proof row, with a content-addressed id.

    The id never digests the *weight*, so re-weighting the taxonomy does not
    fork one observation into two rows.

    **Whether it digests the clock depends on whether the caller named the
    occasion.** With an ``interaction_id``, that id *is* the occasion: two
    recordings of one event from one call are the same observation however far
    apart the retries land, so the timestamp stays out of the digest and an
    at-least-once delivery path is free. Without one there is nothing else to
    tell two occasions apart, so the timestamp goes in -- otherwise a reader
    who selects the same document for the same query on Tuesday and again on
    Friday would be counted once, which under-weights a judgment that was made
    twice.

    Found by running a batch against a real Postgres and re-posting a proof:
    the version that always digested the clock made an agent's retry a second
    row and double-weighted the judgment behind it.
    """

    resolved_query = query_id or make_query_id(query_text or "")
    observed = observed_at or utc_now()
    weight, multipliers = weigh(
        policy,
        event_type,
        observed_at=observed,
        source_reliability=source_reliability,
    )
    proof_id = "proof-" + digest(
        kb_id,
        resolved_query,
        target_type,
        target_id,
        event_type,
        interaction_id,
        observed if not interaction_id else None,
        position,
    )
    return Proof(
        proof_id=proof_id,
        kb_id=kb_id,
        query_id=resolved_query,
        target_type=target_type,
        target_id=str(target_id),
        event_type=event_type,
        polarity=policy.polarity_of(event_type),
        strength=policy.strength_of(event_type),
        weight=weight,
        observed_at=observed,
        interaction_id=interaction_id,
        snapshot_id=snapshot_id,
        principal_partition=principal_partition,
        position=position,
        exposed=exposed,
        outcome_reference=outcome_reference,
        reason_code=reason_code,
        multipliers=multipliers,
    )


@dataclass
class TargetEvidence:
    """The accumulated evidence about one target under one query."""

    target_id: str
    positive: float = 0.0
    negative: float = 0.0
    proof_ids: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    best_strength: str = Strength.WEAK.value
    exposures: int = 0

    @property
    def net(self) -> float:
        return round(self.positive - self.negative, 6)

    def conflicted(self, floor: float) -> bool:
        return self.positive >= floor and self.negative >= floor


@dataclass
class QueryEvidence:
    """Everything known about one query's targets. The metric layer's input."""

    query_id: str
    targets: dict[str, TargetEvidence] = field(default_factory=dict)

    def positives(self, floor: float) -> list[str]:
        return sorted(t.target_id for t in self.targets.values() if t.net >= floor)

    def negatives(self, floor: float) -> list[str]:
        return sorted(t.target_id for t in self.targets.values() if t.net <= -floor)

    def judged(self, floor: float) -> set[str]:
        return set(self.positives(floor)) | set(self.negatives(floor))

    def utility(self, target_id: str) -> float:
        target = self.targets.get(target_id)
        return target.net if target else 0.0

    def proof_ids(self) -> list[str]:
        return sorted({pid for t in self.targets.values() for pid in t.proof_ids})

    def has_judgment(self, floor: float) -> bool:
        return bool(self.positives(floor) or self.negatives(floor))


def aggregate(proofs: list[Proof], policy: ProofPolicy) -> dict[str, QueryEvidence]:
    """Fold proof rows into per-query, per-target evidence.

    Positive and negative sums are kept apart the whole way down. A target with
    ``P=1.0`` and ``N=1.0`` has a net of zero and is *conflicted*, which reads
    identically to unknown in any representation that only stores the net --
    and is the state a reviewer most needs to see, because it usually means the
    document is right for one reader and wrong for another.
    """

    out: dict[str, QueryEvidence] = defaultdict(lambda: QueryEvidence(query_id=""))
    for proof in proofs:
        evidence = out[proof.query_id]
        if not evidence.query_id:
            evidence.query_id = proof.query_id
        target = evidence.targets.setdefault(
            proof.target_id, TargetEvidence(target_id=proof.target_id)
        )
        target.proof_ids.append(proof.proof_id)
        target.event_types.append(proof.event_type)
        if proof.exposed:
            target.exposures += 1
        try:
            if Strength(proof.strength).rank > Strength(target.best_strength).rank:
                target.best_strength = proof.strength
        except ValueError:
            pass
        if proof.polarity == Polarity.POSITIVE.value:
            target.positive = round(target.positive + abs(proof.weight), 6)
        elif proof.polarity == Polarity.NEGATIVE.value:
            target.negative = round(target.negative + abs(proof.weight), 6)
        elif policy.unknown_is_negative:
            # Off by default and loudly so: this is the setting that turns
            # exposure into evidence, and a deployment that enables it is
            # measuring something different from what the metric names claim.
            target.negative = round(target.negative + abs(proof.weight), 6)
    return dict(out)


def conflict_rate(evidence: dict[str, QueryEvidence], policy: ProofPolicy) -> tuple[int, int]:
    """``(conflicted targets, targets with any proof)``."""

    total = 0
    conflicted = 0
    for query in evidence.values():
        for target in query.targets.values():
            if target.positive or target.negative:
                total += 1
                if target.conflicted(policy.positive_floor):
                    conflicted += 1
    return conflicted, total


@dataclass
class Sufficiency:
    """Whether a cohort's evidence supports publishing a number at all."""

    sufficient: bool
    eligible_queries: int
    evidenced_queries: int
    independent_interactions: int
    max_single_query_share: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "eligible_queries": self.eligible_queries,
            "evidenced_queries": self.evidenced_queries,
            "independent_interactions": self.independent_interactions,
            "max_single_query_share": self.max_single_query_share,
            "reasons": list(self.reasons),
        }


def assess_sufficiency(
    policy: ProofPolicy,
    *,
    eligible_query_ids: list[str],
    evidence: dict[str, QueryEvidence],
    proofs: list[Proof],
) -> Sufficiency:
    """Apply the configured minimums and say precisely which one failed.

    Reporting *which* condition failed is the point. "Insufficient evidence" on
    its own tells an operator nothing about whether to wait, to instrument a
    surface, or to widen a cohort -- and those are the only three things they
    can do about it.
    """

    eligible = len(eligible_query_ids)
    evidenced = sum(
        1
        for qid in eligible_query_ids
        if qid in evidence and evidence[qid].has_judgment(policy.positive_floor)
    )
    interactions = len({p.interaction_id for p in proofs if p.interaction_id})
    per_query: dict[str, int] = defaultdict(int)
    for proof in proofs:
        if proof.polarity != Polarity.UNKNOWN.value:
            per_query[proof.query_id] += 1
    judged_total = sum(per_query.values())
    share = (max(per_query.values()) / judged_total) if judged_total else 0.0

    reasons: list[str] = []
    if eligible < policy.minimum_eligible_queries:
        reasons.append(f"eligible queries {eligible} < {policy.minimum_eligible_queries}")
    if evidenced < policy.minimum_evidenced_queries:
        reasons.append(f"evidenced queries {evidenced} < {policy.minimum_evidenced_queries}")
    if interactions < policy.minimum_independent_interactions:
        reasons.append(
            f"independent interactions {interactions} < {policy.minimum_independent_interactions}"
        )
    if judged_total and share > policy.maximum_single_query_proof_share:
        reasons.append(
            f"one query supplies {share:.0%} of judged proof "
            f"(> {policy.maximum_single_query_proof_share:.0%})"
        )
    return Sufficiency(
        sufficient=not reasons,
        eligible_queries=eligible,
        evidenced_queries=evidenced,
        independent_interactions=interactions,
        max_single_query_share=round(share, 4),
        reasons=reasons,
    )


# --------------------------------------------------------------------------
# projection from the observation plane


def project_from_interactions(
    state: Any,
    kb_id: str,
    policy: ProofPolicy,
    *,
    since: str | None = None,
    limit: int = 20_000,
) -> list[Proof]:
    """Derive exposure proof from the interaction ledger.

    Exposure only. Every proof minted here is ``served`` -- polarity unknown,
    weight zero by default -- because that is genuinely all the ledger knows:
    it records what came back, not whether it helped. Utility proof has to come
    from a surface where somebody said so, which is what
    :func:`record_interaction_proof` exists for.

    That restraint is the point. It would be trivial (and wrong) to mine
    "appeared at rank 1" as a positive: the resulting metric would improve
    whenever ranking got *more confident*, regardless of whether it got more
    correct, and every experiment run against it would confirm itself.
    """

    clauses = ["kb_id=?", "query_text IS NOT NULL", "query_text <> ''"]
    params: list[Any] = [kb_id]
    if since:
        clauses.append("started_at >= ?")
        params.append(since)
    try:
        rows = state.rows(
            "SELECT id, query_text, started_at, session_id, principal, result_ids_json "
            f"FROM interaction_events WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at, id LIMIT ?",
            (*params, int(limit)),
        )
    except Exception:  # noqa: BLE001 - a region with observation off has no rows
        logger.debug("evaluation: interaction ledger unavailable", exc_info=True)
        return []

    out: list[Proof] = []
    for row in rows:
        try:
            result_ids = [str(item) for item in json.loads(row["result_ids_json"] or "[]")]
        except (TypeError, ValueError):
            result_ids = []
        partition = partition_token(kb_id, row["principal"], row["session_id"])
        for position, target_id in enumerate(result_ids, start=1):
            out.append(
                make_proof(
                    kb_id=kb_id,
                    query_text=str(row["query_text"]),
                    target_type=TargetType.ARTIFACT.value,
                    target_id=target_id,
                    event_type="served",
                    policy=policy,
                    observed_at=str(row["started_at"]),
                    interaction_id=str(row["id"]),
                    principal_partition=partition,
                    position=position,
                    reason_code="ledger_exposure",
                )
            )
    return out


def project_from_admitted_candidates(
    state: Any, kb_id: str, policy: ProofPolicy, *, limit: int = 1000
) -> list[Proof]:
    """Positive proof from candidates a person admitted into memory.

    The nearest thing to ground truth the region owns without instrumenting a
    new surface: somebody looked at the evidence behind a question and said
    "yes, remember that". ``pheasant eval bootstrap`` already treats an
    admitted candidate as a case's answer, and this reuses that judgment as
    what it is -- an explicit human accept, recorded against the record it
    produced.

    Strength is ``explicit_accept``, not ``deterministic_validation_pass``: a
    person approving a proposal is strong evidence and not a passing test, and
    the two are separated everywhere else in this module.
    """

    try:
        candidates = state.list_memory_candidates(status="admitted", limit=int(limit))
    except Exception:  # noqa: BLE001 - formation is optional
        logger.debug("evaluation: memory candidates unavailable", exc_info=True)
        return []

    out: list[Proof] = []
    for candidate in candidates:
        record_id = candidate.get("record_id")
        if not record_id:
            continue
        try:
            evidence = json.loads(candidate.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            continue
        query_text = evidence.get("query")
        if not query_text:
            continue
        out.append(
            make_proof(
                kb_id=kb_id,
                query_text=str(query_text),
                target_type=TargetType.MEMORY.value,
                target_id=str(record_id),
                event_type="explicit_accept",
                policy=policy,
                observed_at=str(candidate.get("decided_at") or candidate.get("last_seen") or ""),
                principal_partition=partition_token(kb_id, candidate.get("written_by"), None),
                reason_code="candidate_admitted",
            )
        )
    return out


def partition_token(kb_id: str, principal: Any, session: Any) -> str | None:
    """An opaque, stable security partition token -- never an identity.

    Proof is scoped to the partition of the interaction that produced it, and
    a partition has to be comparable across rows (two proofs from one principal
    belong together) without being readable (the file must not answer "what did
    Ada ask"). A keyed digest gives both. The key is the knowledge-base id
    rather than a random salt precisely because stability is the requirement
    here -- unlike ``evalset``, where the export is passed around and a
    per-export salt is what stops two exports being joined.
    """

    if not principal and not session:
        return None
    return "part-" + digest(kb_id, principal or "", session or "")
