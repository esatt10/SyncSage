"""Stage health from live traffic, between batches.

The tuning plane's diagnosis is computed by replaying a cohort. That is the
right way to answer "which stage lost the document", because it needs a known
positive to have lost — but it means a region's picture of its own retrieval is
only as fresh as its last batch, and a regression introduced by an applied
bundle stays invisible until somebody runs another one.

This reads the sampled stage digests off the interaction ledger instead. No
cohort, no replay, no proof: just what the pipeline did on real traffic.

**What that can and cannot say** is the whole design constraint here.

It *can* say: how often each arm contributed, how often a search returned
nothing and which stage still had candidates when it did, how much each filter
removed, how often the fused list was longer than what was returned. Every one
of those is a statement about the machinery, with a denominator, and none of
them needs anybody to have judged an answer.

It *cannot* say whether retrieval was correct. Nobody told these queries what
the right answer was. So every metric here is classified ``structural`` — the
evaluation plane's word for "says what the system did, not whether it helped" —
and none of them may enter a factual-accuracy claim. Mining "this was served at
rank 1" out of live traffic as a *positive* would produce a number that
improves whenever ranking gets more confident regardless of whether it gets
more correct, which is the one shape this repository has decided repeatedly not
to build.

The honest use is a **change detector**. An empty-rate that moves from 3% to
14% after a bundle is applied is a fact about the bundle, and it is actionable
without anyone having judged a single result. That is what makes this worth
having between batches, and it is why the health payload always reports the
bundle each sample ranked under.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pheasant.search.observability import ARMS, EMPTY_STAGES

logger = logging.getLogger(__name__)

#: Below this many samples nothing is reported at all. A rate over nine
#: searches is noise wearing a percentage sign, and publishing it invites
#: somebody to act on it — the same `insufficient_evidence`-rather-than-`0.0`
#: rule the evaluation plane's metrics follow.
MINIMUM_SAMPLES = 25


def stage_health(
    state: Any,
    kb_id: str,
    *,
    since: str | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    """Pipeline behaviour over recently sampled searches.

    Reads only rows that actually carry a digest, and says how many that was —
    a rate whose denominator is invisible is a rate nobody can argue with.
    """

    rows = _sampled_rows(state, kb_id, since=since, limit=limit)
    samples = len(rows)
    if samples < MINIMUM_SAMPLES:
        return {
            "status": "insufficient_evidence",
            "samples": samples,
            "minimum_samples": MINIMUM_SAMPLES,
            "reason": (
                f"{samples} sampled searches; {MINIMUM_SAMPLES} needed before a rate "
                "says anything. Raise observability.interactions.stage_sample_rate, "
                "or wait for traffic."
            ),
            "arms": {},
            "empty": {},
            "bundles": {},
        }

    empty_by_stage: dict[str, int] = dict.fromkeys(EMPTY_STAGES, 0)
    arm_present = dict.fromkeys(ARMS, 0)
    arm_contributed = dict.fromkeys(ARMS, 0)
    arm_failed = dict.fromkeys(ARMS, 0)
    filtered: dict[str, int] = {}
    truncated = 0
    returned_total = 0
    empty_total = 0
    bundles: dict[str, int] = {}

    for digest in rows:
        returned = int(digest.get("returned") or 0)
        returned_total += returned
        if not returned:
            empty_total += 1
            stage = str(digest.get("empty_stage") or "fusion")
            empty_by_stage[stage] = empty_by_stage.get(stage, 0) + 1
        for arm, count in (digest.get("arms") or {}).items():
            if arm not in arm_present:
                continue
            arm_present[arm] += 1
            if int(count or 0) > 0:
                arm_contributed[arm] += 1
        for arm in digest.get("arms_failed") or []:
            if arm in arm_failed:
                arm_failed[arm] += 1
        for name, dropped in (digest.get("filtered") or {}).items():
            filtered[name] = filtered.get(name, 0) + int(dropped or 0)
        if digest.get("truncated"):
            truncated += 1
        key = str(digest.get("bundle_id") or "") or f"({digest.get('provenance') or 'config'})"
        bundles[key] = bundles.get(key, 0) + 1

    return {
        "status": "measured",
        "classification": "structural",
        "samples": samples,
        "since": since or "",
        # Every rate carries the count it came from, because "8% empty" and
        # "8% empty over 25 searches" are different claims and only the second
        # one is checkable.
        "empty": {
            "count": empty_total,
            "rate": empty_total / samples,
            "by_stage": {
                stage: {"count": n, "share": (n / empty_total) if empty_total else None}
                for stage, n in empty_by_stage.items()
                if n
            },
        },
        "arms": {
            arm: {
                "observed": arm_present[arm],
                "contributed": arm_contributed[arm],
                # Of the searches where this arm ran, how often it returned
                # anything. A vector arm at 0.02 is a stale or mis-dimensioned
                # index, and it is costing latency on every hybrid search.
                "contribution_rate": (
                    arm_contributed[arm] / arm_present[arm] if arm_present[arm] else None
                ),
                "failed": arm_failed[arm],
            }
            for arm in ARMS
            if arm_present[arm]
        },
        "filters": {
            name: {"dropped": n, "per_search": n / samples} for name, n in sorted(filtered.items())
        },
        "truncation": {"count": truncated, "rate": truncated / samples},
        "results_per_search": returned_total / samples,
        # Which configuration these samples ranked under. A health payload
        # spanning an apply is two populations, and reporting it as one would
        # average away exactly the change somebody is looking for.
        "bundles": bundles,
        "mixed_configurations": len(bundles) > 1,
        "does_not_support": (
            "Says what the pipeline did, never whether an answer was correct. "
            "Nobody judged these queries, so nothing here is evidence of "
            "retrieval quality — it is a change detector, and its value is "
            "that a shift after an applied bundle is visible without waiting "
            "for a batch."
        ),
    }


def _sampled_rows(state: Any, kb_id: str, *, since: str | None, limit: int) -> list[dict[str, Any]]:
    """The stage digests, newest first. Empty when observation is off."""

    sql = (
        "SELECT attributes_json FROM interaction_events "
        "WHERE kb_id = ? AND attributes_json IS NOT NULL"
    )
    params: list[Any] = [kb_id]
    if since:
        sql += " AND started_at >= ?"
        params.append(since)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        rows = state.rows(sql, tuple(params))
    except Exception:  # noqa: BLE001 - no ledger is the ordinary case
        logger.debug("stage health: no interaction ledger", exc_info=True)
        return []
    digests: list[dict[str, Any]] = []
    for row in rows:
        try:
            attributes = json.loads(row["attributes_json"] or "{}")
        except (TypeError, ValueError):
            continue
        digest = attributes.get("retrieval_stages")
        if isinstance(digest, dict):
            digests.append(digest)
    return digests


def compare_health(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Two health payloads, differenced. The change-detector's whole point.

    Deltas only where **both** sides were measured. Differencing against an
    `insufficient_evidence` payload would produce a number that looks like a
    regression and is actually an absence of traffic.
    """

    if before.get("status") != "measured" or after.get("status") != "measured":
        return {
            "status": "insufficient_evidence",
            "reason": "one side has too few samples to compare",
        }
    empty_delta = after["empty"]["rate"] - before["empty"]["rate"]
    arms = {}
    for arm, stats in after.get("arms", {}).items():
        prior = before.get("arms", {}).get(arm)
        if not prior or stats["contribution_rate"] is None or prior["contribution_rate"] is None:
            continue
        arms[arm] = stats["contribution_rate"] - prior["contribution_rate"]
    return {
        "status": "measured",
        "samples": {"before": before["samples"], "after": after["samples"]},
        "empty_rate_delta": empty_delta,
        "arm_contribution_delta": arms,
        "truncation_rate_delta": after["truncation"]["rate"] - before["truncation"]["rate"],
        "results_per_search_delta": after["results_per_search"] - before["results_per_search"],
    }
