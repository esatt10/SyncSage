"""Memory that improves *every* query, not just the ones it answers (Step 33.8).

Through 33.7 memory was content: something to retrieve. This is the other half
— a record whose payload is not prose to return but a deterministic *retrieval
rule*. It is the cheapest large win available, because it changes ranking on
queries that return no memory at all.

Three kinds, each parsed from the record's own text:

* ``alias``      — ``router -> pheasant-flock, flock``. Expands the query, so
  asking about "the router" finds documents that only ever say
  "pheasant-flock". This is the deterministic form of the learned query
  rewriting the agentic-search literature builds with a policy network; an
  agent that already knows the synonym just writes it down.
* ``preference`` — ``when: deploy, docker -> prefer: docs/, deploy/``. Adds a
  path prior alongside the existing depth / tests / samples ones.
* ``exclusion``  — ``never: vendor/**``. Suppresses candidates outright.

**Scope gating is a security property, not a nicety.** A rule applies only to a
query whose `MemoryPolicy` admits the record's scope, so `session` steering is
confined to that session and `user` steering to that principal. Without it one
agent's note silently re-ranks another's searches — and `org` steering, which
*is* fleet-wide, is the reason that has to be deliberate rather than incidental.

Rule 1 holds by construction: parsing is regex over text an agent already
wrote, no model and no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from pheasant.memory.policy import STEERING_KINDS, MemoryPolicy, admits

#: Record kinds that carry a rule rather than an assertion. Canonical
#: definition lives in `policy` (which cannot import this module); re-exported
#: here because this is where readers look for it.
__all__ = ["STEERING_KINDS", "Steering", "load_steering", "load_steering_records", "parse_rule"]

#: Bounds. A steering set is meant to be a handful of deliberate rules; these
#: stop a runaway store from turning every query into a thousand-term
#: expansion, which would cost far more than it could ever return.
MAX_ALIAS_TERMS = 8
MAX_RULES = 64

_ARROW = re.compile(r"\s*(?:->|→|=>)\s*")
_WHEN = re.compile(
    r"\bwhen\s*:\s*(?P<when>[^\n]*?)\s*(?:->|→|=>)\s*prefer\s*:\s*(?P<prefer>[^\n]+)",
    re.I,
)
_NEVER = re.compile(r"\bnever\s*:\s*(?P<never>[^\n]+)", re.I)


def _terms(value: str) -> list[str]:
    """Split a comma/semicolon list, lowercased, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;]", value or ""):
        term = part.strip().strip("`\"'").lower()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


@dataclass(frozen=True)
class Steering:
    """Every rule in force for one query, already scope-filtered."""

    #: query term -> extra terms to search for as well.
    aliases: dict[str, list[str]] = field(default_factory=dict)
    #: (trigger terms, preferred path prefixes) pairs.
    preferences: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = ()
    #: path fragments to suppress.
    exclusions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.aliases or self.preferences or self.exclusions)

    def expand(self, tokens: list[str]) -> list[str]:
        """Query tokens plus any alias expansions they trigger.

        Additive only: the original tokens always survive, so an alias can
        widen a search but never narrow one. Deterministic order, because the
        expansion feeds an FTS `MATCH` string that must be stable.

        A trigger is matched against the *tokenized* query the same way
        `prefers` matches its own, and for the same reason: this looked up
        `aliases[token]` one token at a time, so a multi-word trigger — the
        natural way to write one, `filewatch daemon -> fileService, watcher` —
        could never match any single token and silently never fired. Every
        part must be present, so a rule about `filewatch daemon` does not fire
        on a query that merely said `daemon`.
        """
        if not self.aliases:
            return tokens
        extra: set[str] = set()
        present = set(tokens)
        for trigger, values in self.aliases.items():
            parts = {part for part in re.split(r"[\s_\-]+", trigger) if len(part) > 1}
            if trigger not in present and not (parts and parts <= present):
                continue
            for value in values:
                for word in re.split(r"[\s_\-]+", value):
                    word = word.strip().lower()
                    if word and len(word) > 1 and word not in present:
                        extra.add(word)
        return sorted(present | extra)

    def prefers(self, query_tokens: list[str]) -> tuple[str, ...]:
        """Path prefixes preferred for this query, or empty.

        A trigger is compared against the *tokenized* query, so a hyphenated
        or underscored term written naturally in a rule still matches: the
        tokenizer splits `pheasant-flock` into `pheasant` and `flock`, and a
        trigger compared whole would never have fired. Every part must be
        present — matching on any one would let a rule about `pheasant-flock`
        fire on a query that merely said `pheasant`.
        """
        wanted = set(query_tokens)
        out: list[str] = []
        for triggers, paths in self.preferences:
            for trigger in triggers:
                parts = {part for part in re.split(r"[\s_\-]+", trigger) if len(part) > 1}
                if trigger in wanted or (parts and parts <= wanted):
                    out.extend(paths)
                    break
        return tuple(dict.fromkeys(out))

    def describe(self) -> dict[str, Any]:
        """What an agent is told is in force, via `describe_retrieval`."""
        return {
            "aliases": {key: list(value) for key, value in sorted(self.aliases.items())},
            "preferences": [
                {"when": list(triggers), "prefer": list(paths)}
                for triggers, paths in self.preferences
            ],
            "exclusions": list(self.exclusions),
        }


def parse_rule(kind: str, text: str) -> tuple[str, Any] | None:
    """One record's text into a rule, or None when it does not parse.

    A malformed rule is **ignored, never raised**: these are written by agents
    into a store that is also read during a sync, and one bad line must not be
    able to fail indexing or a search.
    """
    text = (text or "").strip()
    if not text:
        return None

    if kind == "alias":
        left, _, right = text.partition("\n")[0].partition("->")
        if not right:
            parts = _ARROW.split(text.splitlines()[0], maxsplit=1)
            if len(parts) != 2:
                return None
            left, right = parts
        term = left.strip().strip("`\"'").lower()
        values = _terms(right)[:MAX_ALIAS_TERMS]
        if not term or not values:
            return None
        return ("alias", (term, values))

    if kind == "preference":
        match = _WHEN.search(text)
        if not match:
            return None
        triggers = tuple(_terms(match.group("when")))
        paths = tuple(_terms(match.group("prefer")))
        if not triggers or not paths:
            return None
        return ("preference", (triggers, paths))

    if kind == "exclusion":
        match = _NEVER.search(text)
        if not match:
            return None
        paths = tuple(_terms(match.group("never")))
        return ("exclusion", paths) if paths else None

    return None


def load_steering_records(state: Any) -> list[dict[str, Any]]:
    """Steering records with their text, or `[]`.

    Joined back to `chunks` because the projection stores a record's *metadata*
    and the rule lives in its prose. Restricted to non-`fact` kinds in SQL, so
    a store of ordinary memories costs one indexed lookup that returns nothing
    rather than a scan of every record's body.
    """
    try:
        rows = state.rows(
            "SELECT m.record_id, m.scope, m.subject, m.kind, m.valid_from, m.valid_until, "
            "GROUP_CONCAT(c.text, ' ') AS text "
            "FROM memory_records m JOIN chunks c ON c.artifact_id = m.artifact_id "
            "WHERE m.kind <> 'fact' GROUP BY m.record_id"
        )
    except Exception:  # pragma: no cover - state store older than 33.5
        return []
    return [dict(row) for row in rows]


def load_steering(
    records: list[dict[str, Any]],
    policy: MemoryPolicy,
    *,
    now: str,
    enabled: bool = False,
) -> Steering:
    """The rules a given query may use, or an empty `Steering`.

    `records` are `memory_records` rows carrying `kind` and `text`. Each is put
    through the *same* `admits` predicate that decides whether the record could
    be retrieved at all — so a rule from a corrected, expired or out-of-scope
    record steers nothing, and the two can never disagree about what a query is
    allowed to see.

    The one part of the policy deliberately **not** applied is `mode`.
    `memory: "off"` is a statement about what appears in the results, not about
    which rules are valid: a caller who does not want remembered *content*
    still wants the region's remembered *vocabulary*, and in fact steering's
    whole value is that it improves queries returning no memory at all. Scope,
    subject and validity all still apply.
    """
    if not enabled or not records:
        return Steering()
    # `include_rules` is a statement about what is *returned as content* —
    # steering records are machinery, so they stay out of result lists by
    # default. Here we are deciding which rules are in force, which is the one
    # place that must see them, so the content filter is lifted while scope,
    # subject and validity gating stay exactly as they were.
    rule_policy = replace(policy, mode="auto", include_rules=True)

    aliases: dict[str, list[str]] = {}
    preferences: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    exclusions: list[str] = []

    for record in sorted(records, key=lambda r: str(r.get("record_id") or ""))[:MAX_RULES]:
        kind = str(record.get("kind") or "")
        if kind not in STEERING_KINDS:
            continue
        if not admits(rule_policy, record, now=now):
            continue
        parsed = parse_rule(kind, str(record.get("text") or ""))
        if parsed is None:
            continue
        name, value = parsed
        if name == "alias":
            term, values = value
            aliases.setdefault(term, [])
            for item in values:
                if item not in aliases[term]:
                    aliases[term].append(item)
        elif name == "preference":
            preferences.append(value)
        else:
            exclusions.extend(value)

    return Steering(
        aliases=aliases,
        preferences=tuple(preferences),
        exclusions=tuple(dict.fromkeys(exclusions)),
    )
