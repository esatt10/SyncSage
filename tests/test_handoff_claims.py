"""CLAUDE.md's countable claims, derived instead of remembered.

CLAUDE.md declares itself canonical and is the first thing any contributor or
agent reads. Its own rule is "where docs and code disagree, the code is
authoritative" — which is right, and which also means every stale line is a
trap for the reader who cannot check.

Two of its claims had drifted, and the costly one is the fleet's:
`pheasant serve --role api|indexer|worker|all` omitted `graph` and `logger`,
both of which are shipped tiers with their own manifests. Someone reasoning
about the fleet from that line would not know the graph service exists — which
is the one omission that changes what a person *builds*, not merely what they
believe.

So the countable claims are checked against the code that defines them, in the
same spirit as `tests/test_version_alignment.py`. Deliberately narrow: this
does not try to validate prose. It checks the things that are a set or a
number somewhere in the source, because those are exactly the claims a reader
has no way to sanity-check and every reason to trust.
"""

from __future__ import annotations

import re

import pytest

from pheasant.deployment.roles import POLICIES, Role
from tests.conftest import REPO_ROOT

HANDOFF = REPO_ROOT / "CLAUDE.md"
TESTS = REPO_ROOT / "tests"


@pytest.fixture(scope="module")
def handoff() -> str:
    return HANDOFF.read_text(encoding="utf-8")


def test_the_documented_roles_are_the_roles_that_exist(handoff: str) -> None:
    """The omission the review caught: a fleet reader could not see `graph`.

    Asserted against `Role` rather than a hand-written list here, so adding a
    tier makes this fail until the hand-off says so — which is the only moment
    anyone will remember to write it down.
    """

    line = next(
        (row for row in handoff.splitlines() if row.startswith("pheasant serve --role ")),
        None,
    )
    assert line, "CLAUDE.md no longer documents `pheasant serve --role`"

    documented = set(line.split("--role ", 1)[1].split()[0].split("|"))
    assert documented == {role.value for role in Role}, (
        f"CLAUDE.md documents roles {sorted(documented)}; the code defines "
        f"{sorted(role.value for role in Role)}. A tier missing from the hand-off is a tier "
        "nobody planning a fleet from it knows about."
    )


def test_every_role_the_code_defines_has_a_policy() -> None:
    """A role in the enum with no policy is a `KeyError` at startup, on a pod."""

    assert set(POLICIES) == set(Role)


def test_the_test_module_count_is_current(handoff: str) -> None:
    """A number nothing recomputes is a number that rots.

    Not important on its own — nobody makes a decision on the count — but it is
    the cheapest possible demonstration that the file's countable claims are
    checked at all, and it was 31 modules out of date.
    """

    claimed = re.search(r"←\s*(\d+)\s*pytest modules", handoff)
    assert claimed, "CLAUDE.md no longer states a test-module count"
    actual = len(list(TESTS.glob("test_*.py")))
    assert int(claimed.group(1)) == actual, (
        f"CLAUDE.md says {claimed.group(1)} pytest modules; there are {actual}."
    )


def test_the_repository_layout_names_modules_that_exist(handoff: str) -> None:
    """The layout block is a map, and a map to a file that moved is worse than
    no map: it sends a reader looking in the wrong place with confidence."""

    block = handoff.split("```", 2)[1]
    named = {
        match.group(1)
        for match in re.finditer(r"([a-z_]+(?:/[a-z_]+)*\.py)\b", block)
        if not match.group(1).startswith(("docker", "mkdocs"))
    }
    missing = sorted(
        name
        for name in named
        if not (REPO_ROOT / "src" / "pheasant" / name).exists()
        and not (REPO_ROOT / name).exists()
        and not list(REPO_ROOT.rglob(name))
    )
    assert not missing, f"CLAUDE.md's layout names files that do not exist: {missing}"


def test_the_scale_axes_name_metrics_the_code_publishes(handoff: str) -> None:
    """The scale table tells an operator what to autoscale on.

    A metric named there and never registered is a scaling policy that reads
    zero forever — the failure this whole area exists to make impossible, and
    one that looks exactly like "there is no load".
    """

    from pheasant.telemetry import metrics

    metrics.register_default_metrics("test")
    published = set(metrics.REGISTRY._metrics)  # noqa: SLF001 - the registry is the source

    referenced = set(re.findall(r"`(pheasant_[a-z_]+)`", handoff))
    assert referenced, "CLAUDE.md names no metrics at all"
    missing = sorted(name for name in referenced if name not in published)
    assert not missing, (
        f"CLAUDE.md names metrics that nothing registers: {missing}. A scaling policy "
        "pointed at one of these would read zero forever."
    )
