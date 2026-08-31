from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_graph_workspace_defaults_to_concentric_layout() -> None:
    source = (REPO_ROOT / "ui" / "src" / "state" / "session.tsx").read_text(encoding="utf-8")
    initial = re.search(r"const INITIAL: SessionState = \{(?P<body>.*?)\n\};", source, re.DOTALL)

    assert initial is not None
    assert re.search(r'^\s*layout: "concentric",$', initial.group("body"), re.MULTILINE)


# ---------------------------------------------------------------------------
# The evaluation page.
#
# Mechanical checks against the *source*, in the same spirit as the layout
# assertion above: the UI has no test runner in this repository, so what can be
# guarded cheaply from Python is guarded here. These catch the failures that
# would otherwise reach a browser — a page unreachable from the nav, a client
# method pointing at an endpoint that does not exist, and the two rendering
# rules that keep an evaluation number honest.
# ---------------------------------------------------------------------------

EVALUATION_PAGE = REPO_ROOT / "ui" / "src" / "pages" / "EvaluationPage.tsx"
EVALUATION_PRIMITIVES = REPO_ROOT / "ui" / "src" / "evaluation" / "primitives.tsx"


def test_the_evaluation_page_is_routed_and_reachable() -> None:
    """A page nobody can navigate to is a page that does not exist."""

    app = (REPO_ROOT / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'path="/evaluation"' in app
    assert "EvaluationPage" in app

    topbar = (REPO_ROOT / "ui" / "src" / "components" / "TopBar.tsx").read_text(encoding="utf-8")
    assert 'to="/evaluation"' in topbar


def test_every_evaluation_client_method_points_at_a_real_endpoint() -> None:
    """The UI's client is hand-written against the HTTP surface. A method
    naming a route that does not exist fails in a browser, at the moment
    somebody clicks — which is the worst place to find out."""

    client = (REPO_ROOT / "ui" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    app = (REPO_ROOT / "src" / "pheasant" / "api" / "app.py").read_text(encoding="utf-8")

    referenced = set(re.findall(r"`(/evaluation/[a-z_]+)", client))
    assert referenced, "the client no longer calls any evaluation endpoint"
    for route in sorted(referenced):
        assert f'"{route}"' in app, f"{route} is called by the UI but not served by the API"


def test_the_page_polls_progress_only_while_a_batch_is_running() -> None:
    """A page that polls forever keeps a laptop fan on for a report nobody is
    watching change."""

    source = EVALUATION_PAGE.read_text(encoding="utf-8")
    assert "refetchInterval" in source
    assert 'query.state.data?.status === "running"' in source


def test_a_value_is_never_rendered_without_its_denominator() -> None:
    """ "0.89" and "0.89 over 5 of 103 evidenced queries" are different claims,
    and only the second one is true."""

    source = EVALUATION_PRIMITIVES.read_text(encoding="utf-8")
    assert "denominatorLabel" in source
    assert "eval-tile__denominator" in source
    # And the stylesheet must not shrink it into a footnote.
    styles = (REPO_ROOT / "ui" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".eval-tile__denominator" in styles


def test_an_unmeasured_metric_renders_as_a_gap_not_a_zero() -> None:
    """A red bar describing an instrumentation gap teaches people to ignore red
    bars. This is the single most important rendering rule on the page."""

    source = EVALUATION_PRIMITIVES.read_text(encoding="utf-8")
    formatter = re.search(
        r"export function formatValue\([^)]*\)[^{]*\{(?P<body>.*?)\n\}", source, re.DOTALL
    )
    assert formatter is not None
    body = formatter.group("body")
    # The null check comes first, and it returns an em dash rather than a number.
    assert re.search(r'if \(entry\.value === null[^)]*\) return "—";', body)


def test_gates_are_rendered_apart_from_scores() -> None:
    """An ACL leak is not a low number, it is a stop. It must not sit in the
    same grid as the tiles or share their gradient."""

    source = EVALUATION_PAGE.read_text(encoding="utf-8")
    assert "eval-gates" in source
    assert "Hard gates" in source
    # The tiles grid and the gate list are different containers.
    styles = (REPO_ROOT / "ui" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".eval-tiles {" in styles
    assert ".eval-gates {" in styles


def test_the_page_explains_an_interrupted_run_rather_than_spinning() -> None:
    """The container-stopped case, rendered. A progress bar that keeps spinning
    for a batch nobody is running is the failure the durable row exists to
    prevent, and the page has to say so in words."""

    source = EVALUATION_PAGE.read_text(encoding="utf-8")
    assert 'status.status === "interrupted"' in source
    assert "resumes from there" in source
    assert "attempts" in source
