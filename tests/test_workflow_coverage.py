"""The scale-out backend must be tested on every change, not on some of them.

Postgres is a *selectable backend* (rule 7) and therefore the one with the
weaker signal: the offline suite runs on SQLite, so a dialect divergence is
invisible here by construction. The project's own history is the argument —
a declared FK a maintenance path deliberately violates, a discarded
`cursor.rowcount`, and a SQLite-only `INSERT OR IGNORE`, none of which the
offline suite could see and all three of which one real Postgres run found on
the first try.

Postgres used to run only in the evaluation, memory and tuning workflows, each
behind a `paths:` filter. That made backend coverage a function of which files
a change happened to touch, and the gap was not hypothetical: three of the
modules carrying dialect branching matched no filter at all, among them
`sync/locks.py` — the per-source lease that several indexers rest on, and the
exact file where a `rowcount` divergence once made a claim fail on SQLite and
pass on Postgres.

These tests pin the fix rather than the symptom. They do not enumerate which
modules need Postgres — a curated list is what went stale the first time. They
assert that *some* job runs the suite against a real Postgres on every pull
request, with nothing narrowing it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: Constructs that behave differently, or do not exist, across the two
#: dialects. Deliberately the same grep an operator would run.
DIALECT_MARKERS = re.compile(
    r"backend ==|is_postgres|ON CONFLICT|GREATEST|NULLIF|ts_rank|INSERT OR IGNORE"
)

DSN_VARIABLE = "PHEASANT_TEST_POSTGRES_DSN"


def _workflow(name: str) -> dict[str, Any]:
    # `on:` is the YAML 1.1 boolean True once parsed, which is a trap worth
    # absorbing here rather than in every caller.
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    document["on"] = document.get("on", document.get(True))
    return document


def _pull_request_trigger(document: dict[str, Any]) -> dict[str, Any] | None:
    triggers = document.get("on") or {}
    if not isinstance(triggers, dict):
        return None
    trigger = triggers.get("pull_request")
    return trigger if isinstance(trigger, dict) else ({} if trigger is None else None)


def _runs_postgres(job: dict[str, Any]) -> bool:
    services = job.get("services") or {}
    return any("postgres" in str(spec.get("image", "")) for spec in services.values())


def _sets_the_dsn(job: dict[str, Any]) -> bool:
    for step in job.get("steps") or []:
        if DSN_VARIABLE in (step.get("env") or {}):
            return True
    return DSN_VARIABLE in (job.get("env") or {})


def _unfiltered_postgres_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """(workflow, job name, job) for every Postgres job no path filter narrows."""

    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = _workflow(path.name)
        trigger = _pull_request_trigger(document)
        if trigger is None or trigger.get("paths") or trigger.get("paths-ignore"):
            continue
        for name, job in (document.get("jobs") or {}).items():
            if _runs_postgres(job) and _sets_the_dsn(job):
                found.append((path.name, name, job))
    return found


def test_some_job_runs_postgres_on_every_pull_request() -> None:
    """The property the path filters failed to provide."""

    jobs = _unfiltered_postgres_jobs()
    assert jobs, (
        f"no workflow runs a real Postgres with {DSN_VARIABLE} set on an unfiltered "
        "pull_request trigger. Backend coverage is then a function of which files a "
        "change happens to touch, which is how sync/locks.py — the per-source lease — "
        "ended up with none."
    )


def test_that_job_runs_the_whole_suite_rather_than_a_curated_list() -> None:
    """A list of dialect-sensitive tests is the same staleness, one level down.

    The suite skips its Postgres-gated cases without the DSN and runs them with
    it, so pointing the job at everything costs one extra run and needs no
    maintenance at all.
    """

    for workflow, name, job in _unfiltered_postgres_jobs():
        commands = " ".join(str(step.get("run") or "") for step in job.get("steps") or [])
        assert "pytest" in commands, f"{workflow}:{name} sets the DSN but runs no tests"
        # `pytest -q` with no paths and no `-k`: anything narrower is a list
        # somebody has to remember to update.
        narrowed = re.search(r"pytest[^\n|&]*\s(-k\s|tests/)", commands)
        assert not narrowed, (
            f"{workflow}:{name} narrows the Postgres run to a subset "
            f"({narrowed.group(0).strip()!r}). Run the whole suite: the curated list is "
            "the thing that went stale the first time."
        )


def test_every_dialect_branching_module_is_reached() -> None:
    """The measurement that justified the job, kept live.

    Not a list of modules to maintain — it is derived from the source each
    time — so a new module with a dialect branch is covered the moment it
    exists, and this test says so rather than needing an edit.
    """

    modules = [
        path
        for path in sorted((REPO_ROOT / "src" / "pheasant").rglob("*.py"))
        if DIALECT_MARKERS.search(path.read_text(encoding="utf-8"))
    ]
    assert modules, "no dialect branching found at all — has the seam moved?"

    # An unfiltered job reaches every file by definition; the assertion is that
    # such a job exists, which is what makes the derivation above hold for
    # modules nobody thought about.
    assert _unfiltered_postgres_jobs(), (
        f"{len(modules)} modules carry dialect branching and no unfiltered job "
        "exercises them against Postgres"
    )


@pytest.mark.parametrize("workflow", ["evaluation.yml", "memory.yml", "tuning.yml"])
def test_the_filtered_workflows_keep_their_postgres_leg(workflow: str) -> None:
    """The new job is additive.

    Those three run Postgres against *their* plane's deeper scenarios — a whole
    evaluation batch, a log-tier roll, a tuning experiment — and dropping them
    on the grounds that CI now covers the backend would trade depth for
    breadth. The point was to stop breadth depending on a path filter.
    """

    document = _workflow(workflow)
    matrices = [
        job.get("strategy", {}).get("matrix", {}) for job in (document.get("jobs") or {}).values()
    ]
    assert any("postgres" in str(matrix.get("backend") or "") for matrix in matrices), (
        f"{workflow} no longer runs a postgres backend leg"
    )
