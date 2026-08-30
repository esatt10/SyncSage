"""Regenerate the UI screenshots the README shows.

Repeatable on purpose. A screenshot checked in once and never regenerated is a
picture of a UI that no longer exists — the same rot `sync_version.py` exists
to prevent for image tags — so this seeds a region, drives a real browser
against the real built UI, and overwrites `docs/assets/ui/*.png`.

    npm --prefix ui ci && npm --prefix ui run build
    python scripts/screenshot_ui.py

Everything it shows is produced by the product: the corpus is indexed by the
ordinary pipeline, the memory records are written through `memory_write`, and
the proposals in the Memory tab are mined from real searches this script
performs. Nothing is mocked for the camera.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "docs" / "assets" / "ui"
VIEWPORT = {"width": 1440, "height": 900}

#: A corpus small enough to seed in a second and real enough to search. The
#: vocabulary mismatch is deliberate: the docs say `pheasant-flock`, the
#: searches below say `router`, and that is exactly the pattern
#: `alias-cooccurrence-v1` is built to notice — so the Memory tab's proposals
#: are genuinely mined rather than staged.
#: Enough vocabulary mismatch to produce a review queue worth showing. The
#: Memory tab's job at scale is triage, and a screenshot of three proposals
#: does not show whether the page can do it.
TEAM_WORDS = {
    "router": "pheasant-flock",
    "watcher": "filewatch",
    "runbook": "kestrel",
}

CORPUS = {
    "deploy/rollout.md": (
        "# Rollout\n\n"
        "The pheasant-flock service coordinates every rollout and canary.\n"
        "Promotion waits on the health check before traffic shifts.\n"
    ),
    "deploy/canary.md": (
        "# Canary\n\nCanary steps are driven by the pheasant-flock service before promotion.\n"
    ),
    "runbooks/kestrel.md": (
        "# Kestrel Runbook\n\n"
        "The filewatch daemon restarts nightly at 0300 UTC.\n"
        "Escalate to the on-call rota if it fails twice in a row.\n"
    ),
    "guides/onboarding.md": (
        "# Onboarding\n\nRead the handbook, request access, then pair for a week.\n"
    ),
}

SEARCHES = [
    ("router rollout coordination", "sess-ada"),
    ("router canary promotion", "sess-ada"),
    ("runbook escalation rota", "sess-ada"),
    ("watcher restart schedule", "sess-ada"),
    ("router rollout", "sess-bo"),
    ("router canary steps", "sess-bo"),
    ("runbook oncall", "sess-bo"),
    ("watcher nightly", "sess-bo"),
    ("promotion health gate", "sess-bo"),
    ("promotion health gate", "sess-ada"),
    # Questions the corpus cannot answer. The most useful proposals in the
    # queue: what the region keeps being asked and keeps failing.
    ("vault seal rotation", "sess-ada"),
    ("vault seal rotation", "sess-bo"),
    ("quarterly capacity forecast", "sess-ada"),
    ("quarterly capacity forecast", "sess-bo"),
]

MEMORIES = [
    ("The staging cluster lives in us-east-2.", "org", "infra"),
    ("Rollouts are frozen on Fridays after 1400 UTC.", "org", "deploy"),
    ("Ada owns the kestrel on-call rota.", "org", "people"),
]


def _free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _seed(root: Path) -> Path:
    workspace = root / "workspace" / "handbook"
    for name, body in CORPUS.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for name in ("state", "exports", "memory"):
        (root / name).mkdir(parents=True, exist_ok=True)

    config = {
        "pheasant": {
            "name": "handbook",
            "description": "Engineering handbook, runbooks and deploy notes.",
            "state_path": str(root / "state"),
            "workspace_root": str(root / "workspace"),
            "exports_path": str(root / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "search": {"embeddings": {"provider": "stub"}},
        # On, with no provider: the assistant answers extractively -- the top
        # retrieved passages with their citations. That is a real answer from
        # a real index, and it means the screenshot needs no API key and no
        # network, which is what makes it reproducible.
        "assistant": {"enabled": True, "provider": "none"},
        "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        "observability": {"interactions": {"enabled": True, "flush_batch_size": 1}},
        "memory": {
            "steering_enabled": True,
            "formation": {"enabled": True, "min_observations": 2, "min_sessions": 2},
        },
        "sources": [
            {
                "name": "handbook",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
            {
                "name": "agent-memory",
                "type": "memory",
                "path": str(root / "memory"),
                "sync": {"on_startup": False},
            },
        ],
    }
    path = root / "pheasant.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _drive(app) -> None:
    """Put real content in front of the camera, through the real surfaces."""

    from fastapi.testclient import TestClient

    from pheasant.memory.formation import run_candidate_rules, run_session_digests

    engine = app.state.engine
    engine.sync_source("handbook", "full")

    with TestClient(app) as client:
        for text, scope, subject in MEMORIES:
            client.post("/memory", json={"text": text, "scope": scope, "subject": subject})
        for query, session in SEARCHES:
            client.post(
                "/search",
                json={"query": query, "mode": "hybrid", "max_results": 8},
                headers={"X-Pheasant-Session": session, "X-Pheasant-Principal": "user:ada"},
            )
        app.state.interaction_buffer.flush()

    run_session_digests(engine)
    run_candidate_rules(engine)
    engine.sync_source("agent-memory", "full")


def _shoot(port: int, shots: dict[str, str]) -> None:
    """Navigate the way a person does: load once, then click.

    A hard load of `/memory` does **not** reach the UI. That path is also an
    API route, and FastAPI resolves it before the SPA's static mount -- so
    `goto("/memory")` photographs a JSON body. The React router owns those
    paths only after the app is running, which is exactly what clicking the
    nav does.
    """
    from playwright.sync_api import sync_playwright

    TARGET.mkdir(parents=True, exist_ok=True)
    # An explicit executable when one is provisioned: a pinned Playwright and
    # a pre-installed browser can disagree about the build number, and
    # downloading one in CI is both slow and a network dependency this repo
    # does not otherwise have.
    provisioned = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    launch: dict[str, object] = {}
    if provisioned.exists():
        launch["executable_path"] = str(provisioned)

    with sync_playwright() as play:
        browser = play.chromium.launch(**launch)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Ask something, so the notebook shows an answer rather than an empty
        # composer. A demo that demos.
        composer = page.get_by_placeholder("Ask anything about your sources")
        composer.click()
        composer.fill("What restarts nightly, and who should be paged if it fails?")
        composer.press("Enter")
        page.wait_for_timeout(4000)

        for name, link in shots.items():
            if link:
                page.get_by_role("link", name=link, exact=True).click()
            else:
                page.get_by_role("link", name="Notebook", exact=True).click()
            # The panes hydrate from several queries; a settled network is not
            # the same as a settled layout.
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            if name == "memory":
                # Open the layers. A screenshot of collapsed rows shows the
                # triage view and hides the thing that makes a proposal
                # reviewable at all: what was asked, what came back, the
                # spans behind it, and what is behind one of those keys.
                page.get_by_role("button", name="Show evidence").first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(600)
                page.get_by_role("button", name="Trace").first.click()
                page.wait_for_timeout(500)
                # Layer 4, and the reason to drive it here rather than assert
                # it in a unit test: this is the only place the whole chain
                # runs against a real corpus -- a search records a result id,
                # a rule carries that id into a candidate, and the panel
                # resolves it back to the indexed text. A mock of
                # /nodes/content would prove none of that.
                keys = page.get_by_title("the content-addressed ids this call returned")
                if keys.count():
                    keys.first.click()
                    page.wait_for_timeout(400)
                    content_key = page.locator(".sidepanel__key")
                    if content_key.count():
                        content_key.first.click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(600)
                        resolved = page.locator(".sidepanel__resolved pre").count()
                        print(f"  panel: key selected, resolved={resolved}")
                        if not resolved:
                            raise SystemExit(
                                "the selected content key did not resolve to text -- "
                                "the recorded result ids and /nodes/content disagree"
                            )
                else:
                    raise SystemExit("no content keys on the trace -- nothing to select")
            out = TARGET / f"{name}.png"
            page.screenshot(path=str(out))
            print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="Leave the seeded region on disk for inspection."
    )
    args = parser.parse_args()

    dist = REPO / "ui" / "dist"
    if not (dist / "index.html").exists():
        raise SystemExit(
            "ui/dist is missing. Build it first:\n  npm --prefix ui ci && npm --prefix ui run build"
        )

    import uvicorn

    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig

    root = Path(tempfile.mkdtemp(prefix="pheasant-shots-"))
    try:
        config_path = _seed(root)
        config = PheasantConfig.model_validate(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        )
        # Two app instances over one state directory, on purpose: the MCP
        # SDK's session manager can only run its lifespan once per instance,
        # and seeding through a TestClient consumes that. The served app is a
        # fresh one reading what the first wrote -- which is also a fair
        # reflection of what a browser sees, since it is a cold process.
        _drive(create_app(config, config_path=str(config_path)))
        app = create_app(config, config_path=str(config_path))

        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        else:
            raise SystemExit("the server did not start")

        print(f"Serving the seeded region on 127.0.0.1:{port}")
        _shoot(
            port,
            {
                "notebook": "Notebook",
                "memory": "Memory",
                "sources": "Sources",
                "graph": "Graph",
            },
        )
        server.should_exit = True
        thread.join(timeout=10)
        print(json.dumps({"screenshots": sorted(p.name for p in TARGET.glob("*.png"))}))
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
