"""How much a memory has earned its place (Step 33.9).

Everything else about a record is derivable from its file. Salience is not: it
is the one thing memory learns from *being used*, and it answers the question a
growing store eventually forces — when there are more memories than anyone can
carry, which ones go?

Deterministic by construction. A documented formula over recorded inputs, no
model and no sampling, so the same store always ranks the same way and a
pruning pass is reproducible rather than a judgement call.

Three inputs, each earning its place:

* **recency** — how long ago the record was asserted. Memory is a claim about
  the world and the world moves; an old note is likelier to be wrong.
* **use** — how often retrieval actually returned it. The cheapest available
  evidence that a record answers real questions rather than merely existing.
* **scope** — an `org` fact was written for everyone and a `session` note for
  one conversation, so they should not compete on equal terms for the last
  slot in a bounded store.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

#: Days after which recency has decayed by half. A quarter: long enough that a
#: fact asserted last month is still strong, short enough that a year-old note
#: loses to a fresh one.
HALF_LIFE_DAYS = 90.0

#: Diminishing returns on use. The gap between "never returned" and "returned
#: once" is the informative one; between 40 and 41 it is noise, and a linear
#: count would let one hot record dominate a whole store.
USE_WEIGHT = 0.5

#: What a scope is worth. `org` is a shared assertion, `session` is scratch.
SCOPE_WEIGHT = {"org": 1.25, "user": 1.0, "session": 0.6}


def _age_days(asserted_at: str, now: datetime) -> float:
    if not asserted_at:
        return 0.0
    try:
        moment = datetime.fromisoformat(str(asserted_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (now - moment).total_seconds() / 86400.0)


def salience(record: Any, *, now: datetime | None = None) -> float:
    """Score a record. Higher is more worth keeping.

    `record` is a `memory_records` row (or anything with the same keys).
    """
    instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
    get = record.get if hasattr(record, "get") else lambda key, default=None: default

    age = _age_days(str(get("asserted_at") or ""), instant)
    recency = math.pow(0.5, age / HALF_LIFE_DAYS)

    uses = max(0, int(get("uses") or 0))
    # log1p, not the raw count: see USE_WEIGHT.
    use = 1.0 + USE_WEIGHT * math.log1p(uses)

    weight = SCOPE_WEIGHT.get(str(get("scope") or ""), 1.0)
    return round(recency * use * weight, 6)


def rank(records: list[Any], *, now: datetime | None = None) -> list[Any]:
    """Records most worth keeping first.

    Ties break on `record_id` so the order is total and a pruning pass over an
    unchanged store always removes the same rows.
    """
    instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
    return sorted(
        records,
        key=lambda record: (
            -salience(record, now=instant),
            str(record.get("record_id") if hasattr(record, "get") else ""),
        ),
    )


def over_capacity(records: list[Any], max_records: int | None, *, now: datetime | None = None):
    """The least salient records beyond `max_records`, or `[]`.

    Returns what to archive, and nothing else decides it: `None` means
    unbounded, which is the pre-33.9 behavior and the default.
    """
    if not max_records or max_records <= 0 or len(records) <= max_records:
        return []
    return rank(records, now=now)[max_records:]
