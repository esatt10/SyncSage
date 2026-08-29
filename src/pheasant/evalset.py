"""Building an evaluation case set out of what the region was actually asked.

`memory/benchmark.py` measures recall against a *synthetic* corpus it generates
itself --- deterministic, offline, and completely disconnected from what anyone
really asks a given region. That is the right shape for a regression gate and
the wrong shape for "is this region good at its own job".

The observation plane makes the second question answerable: real queries, from
real principals, with the results the region actually returned and, where a
promotion followed, the record that answered it.

**The export is derived and de-identified, never the raw table.**
:mod:`pheasant.analytics` excludes ``source_audit_events`` and ``idp_groups``
from Parquet exports on principle --- *"who a principal is, which groups they
are in, and what they did. An export is a file people pass around; identity and
audit data is not that."* An interaction ledger is exactly that category, so:

* ``principal`` and ``session_id`` are **hashed**, not copied. Keeping them as
  opaque, stable tokens preserves the only property an eval needs of them (two
  cases from one session are related) while making the file useless for
  answering "what did Ada ask".
* the salt is per-export and random, so two exports cannot be joined against
  each other to re-identify anybody by intersection.
* nothing else leaves: no trace ids, no client ids, no timings.

What remains is a case: a question, the ids and paths the region returned for
it, and whether it found anything at all.
"""

from __future__ import annotations

import json
import secrets
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import blake2b
from pathlib import Path
from typing import Any

#: Bumped when a field is removed, renamed or retyped --- never when one is
#: added, so a harness written against version 1 keeps working as this grows.
EVALSET_FORMAT_VERSION = 1


@dataclass
class EvalCase:
    """One question the region was really asked, and what it really returned."""

    query: str
    modality: str
    #: Stable within one export, meaningless across two. Enough to tell that
    #: two cases came from one conversation; useless for saying whose.
    session: str
    principal: str | None
    expected_ids: list[str] = field(default_factory=list)
    expected_paths: list[str] = field(default_factory=list)
    result_count: int = 0
    top_score: float | None = None
    #: Set when a memory record was promoted from evidence including this
    #: query --- the closest thing the ledger has to a ground-truth answer.
    answered_by_record: str | None = None
    asked: int = 1


def _token(value: str | None, salt: str) -> str | None:
    """A stable, per-export pseudonym. Not reversible, not joinable."""

    if not value:
        return None
    return blake2b(f"{salt}|{value}".encode(), digest_size=8).hexdigest()


def build_cases(
    state: Any,
    *,
    limit: int = 500,
    min_results: int = 0,
    salt: str | None = None,
) -> list[EvalCase]:
    """Collapse the ledger into distinct questions, best-evidenced first.

    Distinct *questions*, not rows: the same query asked forty times is one
    case with `asked: 40`, which is both a smaller file and a better weight
    for a harness that wants to know what matters rather than what repeated.
    """

    salt = salt or secrets.token_hex(16)
    rows = state.rows(
        "SELECT query_text, modality, principal, session_id, result_ids_json, "
        "result_paths_json, result_count, top_score "
        "FROM interaction_events "
        "WHERE query_text IS NOT NULL AND query_text <> '' AND result_count >= ? "
        "ORDER BY started_at, id",
        (int(min_results),),
    )

    merged: dict[str, EvalCase] = {}
    asked: Counter[str] = Counter()
    for row in rows:
        query = str(row["query_text"]).strip()
        if not query:
            continue
        asked[query] += 1
        # Last writer wins on purpose: the most recent answer is the one the
        # region would give now, and an eval against a stale result set
        # measures an index that no longer exists.
        try:
            ids = [str(item) for item in json.loads(row["result_ids_json"] or "[]")]
            paths = [str(item) for item in json.loads(row["result_paths_json"] or "[]")]
        except (TypeError, ValueError):
            ids, paths = [], []
        merged[query] = EvalCase(
            query=query,
            modality=str(row["modality"] or ""),
            session=_token(row["session_id"], salt) or "",
            principal=_token(row["principal"], salt),
            expected_ids=ids,
            expected_paths=paths,
            result_count=int(row["result_count"] or 0),
            top_score=float(row["top_score"]) if row["top_score"] is not None else None,
        )

    for query, case in merged.items():
        case.asked = asked[query]
    ranked = sorted(merged.values(), key=lambda case: (-case.asked, case.query))
    return ranked[: max(1, int(limit))]


def attach_answers(state: Any, cases: list[EvalCase]) -> list[EvalCase]:
    """Mark cases a promoted memory record was formed from.

    The nearest thing to ground truth the ledger can offer: somebody looked at
    the evidence behind this question and said "yes, remember that".
    """

    try:
        candidates = state.list_memory_candidates(status="admitted", limit=500)
    except Exception:  # noqa: BLE001 - an eval set must build without formation
        return cases
    answers: dict[str, str] = {}
    for candidate in candidates:
        try:
            evidence = json.loads(candidate.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            continue
        query = evidence.get("query")
        if query and candidate.get("record_id"):
            answers[str(query)] = str(candidate["record_id"])
    for case in cases:
        case.answered_by_record = answers.get(case.query)
    return cases


def write_cases(cases: list[EvalCase], target: Path) -> dict[str, Any]:
    """Write the case set as JSON, with a manifest a reader can check.

    JSON rather than Parquet: a case set is hundreds of rows, not millions, and
    the thing that reads it is a test harness rather than a query engine. The
    Parquet export exists for the corpus; this is a different artifact with a
    different reader.
    """

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": EVALSET_FORMAT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cases": [asdict(case) for case in cases],
        "counts": {
            "cases": len(cases),
            "answered": sum(1 for case in cases if case.answered_by_record),
            "unanswered": sum(1 for case in cases if case.result_count == 0),
        },
        "note": (
            "Principals and sessions are per-export pseudonyms, not identifiers. "
            "Two exports cannot be joined to re-identify anyone."
        ),
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload["counts"] | {"path": str(target)}


def bootstrap(state: Any, target: Path, *, limit: int = 500) -> dict[str, Any]:
    """The whole pass: read the ledger, build cases, de-identify, write."""

    cases = attach_answers(state, build_cases(state, limit=limit))
    return write_cases(cases, target)
