"""One-line setup over arbitrary targets, and one-line container hosting.

Acceptance:

1. Any target shape resolves to a source: folder, file, markdown notes,
   obsidian vault, git repo (local + remote URL), web URL, s3://,
   connector plugin name.
2. A glob or ``--split`` over a folder-of-folders yields one source per
   child directory, with de-duplicated names.
3. ``pheasant up a b c`` writes one config with all three sources, indexes
   them, and re-running it re-indexes nothing (idempotency spine).
4. ``pheasant host`` generates a compose file plus a container-view config
   whose paths are remapped into the image — without needing Docker.
5. The UI's ``/sources/quick-add`` is the same resolution path over HTTP.
"""

from __future__ import annotations

import argparse
import re
import shutil
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.cli import main
from pheasant.config.loader import load_config
from pheasant.deployment.host import render_compose
from pheasant.targets import (
    ResolvedTarget,
    TargetError,
    expand_specs,
    is_git_url,
    resolve_target,
    resolve_targets,
)

SAMPLE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "sample_workspace"


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / ".pheasant" / "sources", tmp_path / ".pheasant" / "external"


def _sync_counts(output: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in re.findall(r"indexed=(\d+) skipped=(\d+)", output)]


# --------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/anthropics/claude-code",
        "git@github.com:owner/repo.git",
        "https://gitlab.com/group/project",
        "https://example.com/thing.git",
        "ssh://git@host/repo",
    ],
)
def test_git_urls_are_recognised(url: str) -> None:
    assert is_git_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.example.com/guide",
        "https://github.com/owner/repo/blob/main/README.md",  # a page, not a clone
        "https://example.com",
    ],
)
def test_non_git_urls_are_not_clones(url: str) -> None:
    assert is_git_url(url) is False


def test_local_shapes_are_classified(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a", encoding="utf-8")
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "a.pdf").write_bytes(b"%PDF-")
    (mixed / "b.csv").write_text("x", encoding="utf-8")
    single = tmp_path / "one.md"
    single.write_text("# one", encoding="utf-8")

    def kind(path: Path) -> str:
        return resolve_target(str(path), clone_root=clone_root, workspace=workspace).type

    assert kind(vault) == "obsidian_vault"
    assert kind(repo) == "repository"
    assert kind(notes) == "markdown_folder"
    assert kind(mixed) == "document_folder"
    assert kind(single) == "single_file"


def test_remote_and_connector_targets_resolve_without_touching_disk(roots) -> None:
    clone_root, workspace = roots

    repo = resolve_target(
        "https://github.com/owner/proj", clone_root=clone_root, workspace=workspace
    )
    assert repo.type == "repository"
    assert repo.clone_url == "https://github.com/owner/proj"
    assert repo.name == "proj"
    assert repo.path.endswith("proj")

    web = resolve_target(
        "https://docs.example.com/guide", clone_root=clone_root, workspace=workspace
    )
    assert web.type == "web_collection"
    assert web.urls == ["https://docs.example.com/guide"]
    assert web.local is False

    bucket = resolve_target("s3://my-bucket/prefix", clone_root=clone_root, workspace=workspace)
    assert bucket.type == "s3"
    assert bucket.local is False

    # Step 31.1 plugin types pass through by name and resolve at dispatch.
    notion = resolve_target("notion:my-workspace", clone_root=clone_root, workspace=workspace)
    assert notion.type == "notion"
    assert notion.local is False


def test_explicit_type_prefix_overrides_detection(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    folder = tmp_path / "stuff"
    folder.mkdir()
    (folder / "a.md").write_text("# a", encoding="utf-8")

    target = resolve_target(f"docs:{folder}", clone_root=clone_root, workspace=workspace)
    assert target.type == "document_folder"  # not the auto-detected markdown_folder


def test_missing_local_path_is_an_error(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    with pytest.raises(TargetError):
        resolve_target(str(tmp_path / "nope"), clone_root=clone_root, workspace=workspace)


# ------------------------------------------------------------------- collections


def test_glob_and_split_expand_a_folder_of_folders(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "clients"
    for name in ("acme", "globex", "initech"):
        (parent / name).mkdir(parents=True)
        (parent / name / "brief.md").write_text("# brief", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    globbed = expand_specs(["clients/*"])
    assert len(globbed) == 3

    split = expand_specs([str(parent)], split=True)
    assert sorted(Path(p).name for p in split) == ["acme", "globex", "initech"]


def test_duplicate_names_are_disambiguated(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    for parent in ("a", "b"):
        (tmp_path / parent / "docs").mkdir(parents=True)
        (tmp_path / parent / "docs" / "x.md").write_text("# x", encoding="utf-8")

    targets = resolve_targets(
        [str(tmp_path / "a" / "docs"), str(tmp_path / "b" / "docs")],
        clone_root=clone_root,
        workspace=workspace,
    )
    assert [t.name for t in targets] == ["docs", "docs-2"]


# --------------------------------------------------------------------- `up` e2e


def test_up_indexes_multiple_targets_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = tmp_path / "notes"
    shutil.copytree(SAMPLE_WORKSPACE, notes)
    clients = tmp_path / "clients"
    for name in ("acme", "globex"):
        (clients / name).mkdir(parents=True)
        (clients / name / "brief.md").write_text(f"# {name} brief\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["up", str(notes), str(clients / "*"), "--no-serve"]) == 0
    output = capsys.readouterr().out

    config_path = tmp_path / "pheasant.yaml"
    cfg = load_config(config_path)
    assert [s.name for s in cfg.sources] == ["notes", "acme", "globex"]
    assert cfg.sources[1].type.value == "markdown_folder"
    # Every local target is reachable under the security allowlist.
    roots = {str(p) for p in cfg.security.allow_workspace_roots}
    assert str(clients / "acme") in roots

    counts = _sync_counts(output)
    assert len(counts) == 3
    assert all(indexed > 0 for indexed, _ in counts)

    first_bytes = config_path.read_bytes()
    assert main(["up", str(notes), str(clients / "*"), "--no-serve"]) == 0
    second = capsys.readouterr().out
    assert config_path.read_bytes() == first_bytes, "config is never rewritten"
    assert all(indexed == 0 and skipped > 0 for indexed, skipped in _sync_counts(second))


def test_up_splits_a_parent_directory_into_one_source_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "projects"
    for name in ("alpha", "beta"):
        (parent / name).mkdir(parents=True)
        (parent / name / "readme.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["up", str(parent), "--split", "--no-serve"]) == 0
    capsys.readouterr()
    cfg = load_config(tmp_path / "pheasant.yaml")
    assert [s.name for s in cfg.sources] == ["alpha", "beta"]


def test_up_rejects_a_target_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["up", str(tmp_path / "missing"), "--no-serve"]) == 1


def test_generated_multi_source_config_round_trips_through_yaml(tmp_path: Path, roots) -> None:
    """Globs and URLs in a generated config must survive the YAML round trip."""
    from pheasant.quickstart import render_up_config

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a", encoding="utf-8")
    clone_root, workspace = roots
    targets = resolve_targets(
        [str(notes), "https://docs.example.com/guide"],
        clone_root=clone_root,
        workspace=workspace,
    )
    config_path = tmp_path / "pheasant.yaml"

    text = render_up_config(targets, config_path)
    assert text == render_up_config(targets, config_path), "rendering is deterministic"

    raw = yaml.safe_load(text)
    assert [s["name"] for s in raw["sources"]] == ["notes", "docs-example-com"]
    assert raw["sources"][0]["include"] == ["**/*.md", "**/*.markdown"]
    assert raw["sources"][1]["urls"] == ["https://docs.example.com/guide"]


# --------------------------------------------------------------------- `host`


def test_host_generates_compose_and_container_config_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    args = argparse.Namespace(
        path=[str(notes)],
        config="pheasant.yaml",
        name=None,
        port=9100,
        ui_port=9200,
        profile="quickstart",
        split=False,
        output="docker-compose.pheasant.yml",
        image=None,
        ui_image=None,
        no_ui=False,
        print_only=True,
    )
    from pheasant.deployment.host import host_stack

    assert host_stack(args) == 0
    capsys.readouterr()

    compose = yaml.safe_load((tmp_path / "docker-compose.pheasant.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["pheasant"]["ports"] == ["9100:8765"]
    assert services["pheasant-ui"]["ports"] == ["9200:80"]
    # The local source is mounted read-only at a stable container path.
    assert any(v.endswith("/sources/notes:ro") for v in services["pheasant"]["volumes"])
    # State survives `docker compose down`.
    assert any(v.endswith(":/state") for v in services["pheasant"]["volumes"])

    container_config = yaml.safe_load(
        (tmp_path / "pheasant.container.yaml").read_text(encoding="utf-8")
    )
    assert container_config["sources"][0]["path"] == "/sources/notes"
    assert container_config["pheasant"]["state_path"] == "/state"
    assert "/sources/notes" in container_config["security"]["allow_workspace_roots"]


def test_host_pins_the_ui_image_and_builds_it_from_a_checkout(tmp_path: Path) -> None:
    """The UI sidecar must resolve to a real image, and to the current one.

    `latest` is never re-pulled once Docker has it locally, which is how an
    upgraded stack keeps serving a months-old bundle; and a source checkout can
    be ahead of any published tag, so it gets a build context too.
    """
    from pheasant.deployment.host import DEFAULT_UI_IMAGE, local_ui_context
    from pheasant.version import __version__

    assert DEFAULT_UI_IMAGE == f"ghcr.io/esatt10/pheasant-ui:{__version__}"

    target = ResolvedTarget(
        name="notes", type="markdown_folder", path=str(tmp_path / "notes"), description="d"
    )
    kwargs = {
        "config_path": tmp_path / "c.yaml",
        "state_dir": tmp_path / ".pheasant",
        "port": 8765,
        "ui_port": 8080,
    }

    ui = yaml.safe_load(render_compose([target], **kwargs))["services"]["pheasant-ui"]
    assert ui["image"] == DEFAULT_UI_IMAGE
    assert "build" not in ui, "no build stanza without a checkout to build from"

    built = yaml.safe_load(render_compose([target], ui_build_context="/repo/ui", **kwargs))
    assert built["services"]["pheasant-ui"]["build"] == {"context": "/repo/ui"}

    # An explicit --ui-image is a deliberate choice of a published bundle.
    pinned = yaml.safe_load(
        render_compose([target], ui_image="my/ui:1.0", ui_build_context="/repo/ui", **kwargs)
    )
    assert pinned["services"]["pheasant-ui"]["image"] == "my/ui:1.0"
    assert "build" not in pinned["services"]["pheasant-ui"]

    # This repo IS a checkout, so the fallback resolves to its ui/ directory.
    assert local_ui_context() == str(Path(__file__).resolve().parents[1] / "ui")


def test_host_can_omit_the_ui_sidecar(tmp_path: Path) -> None:
    target = ResolvedTarget(
        name="notes", type="markdown_folder", path=str(tmp_path / "notes"), description="d"
    )
    compose = yaml.safe_load(
        render_compose(
            [target],
            config_path=tmp_path / "c.yaml",
            state_dir=tmp_path / ".pheasant",
            port=8765,
            ui_port=8080,
            include_ui=False,
        )
    )
    assert set(compose["services"]) == {"pheasant"}


# --------------------------------------------------------------------- HTTP


def test_quick_add_registers_and_syncs_a_pasted_path(loaded_config, tmp_path: Path) -> None:
    external = tmp_path / "pasted-notes"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nSome content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config))

    response = client.post("/sources/quick-add", json={"target": str(external)})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["name"] == "pasted-notes"
    assert body["sources"][0]["type"] == "markdown_folder"
    assert body["sync_results"][0]["indexed_artifacts"] == 1
    assert any(s["name"] == "pasted-notes" for s in client.get("/sources").json())


def test_quick_add_rejects_a_bad_target(loaded_config, tmp_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config))
    response = client.post("/sources/quick-add", json={"target": str(tmp_path / "nowhere")})
    assert response.status_code == 400


def _wait_until_not_syncing(client: TestClient, name: str, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    record = None
    while time.monotonic() < deadline:
        record = next((s for s in client.get("/sources").json() if s["name"] == name), None)
        if record is not None and not record["syncing"]:
            return record
        time.sleep(0.2)
    raise AssertionError(f"{name} never finished background-syncing within {timeout_s}s")


def test_quick_add_with_wait_false_registers_immediately_and_syncs_in_background(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    """The fix for the UI's "stuck at the form" / 504 report: registering a
    source must return before indexing finishes, and GET /sources must show
    live progress rather than the caller having to hold the connection open
    for however long the first sync takes."""

    external = tmp_path / "pasted-notes-bg"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nSome content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    response = client.post("/sources/quick-add", json={"target": str(external), "wait": False})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["name"] == "pasted-notes-bg"
    # Nothing was awaited — no synchronous sync result yet.
    assert body["sync_results"] == []
    assert body["syncing"] == ["pasted-notes-bg"]

    # Visible as syncing immediately, before the background thread has
    # necessarily even started — this is what a client polls right after
    # the registration response to show a live "syncing" badge.
    immediate = next(s for s in client.get("/sources").json() if s["name"] == "pasted-notes-bg")
    assert immediate["syncing"] is True

    settled = _wait_until_not_syncing(client, "pasted-notes-bg")
    assert settled["sync_error"] is None


def test_sync_source_with_wait_false_returns_immediately_and_settles(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    external = tmp_path / "pasted-notes-bg2"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nMore content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    response = client.post("/sync/pasted-notes-bg2", json={"wait": False})

    assert response.status_code == 200
    assert response.json() == {"status": "syncing", "source_id": "pasted-notes-bg2"}
    settled = _wait_until_not_syncing(client, "pasted-notes-bg2")
    assert settled["sync_error"] is None


def test_sync_source_with_wait_false_does_not_start_a_second_overlapping_sync(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    """Regression: found live — a source with no checkpoint yet (every
    attempt does a full pass) got a background sync started twice in close
    succession (a container-startup trigger landing on top of a manual
    one), running two concurrent embedding-heavy passes over the same
    source. That tripped the OpenAI embeddings endpoint's rate limit and
    duplicated disk/CPU work for nothing. A second `wait: false` trigger
    for a source that is already syncing must be a no-op, not a second
    thread."""

    external = tmp_path / "pasted-notes-bg4"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nStill more content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    first = client.post("/sync/pasted-notes-bg4", json={"wait": False})
    second = client.post("/sync/pasted-notes-bg4", json={"wait": False})

    assert first.json()["status"] == "syncing"
    assert second.json() == {"status": "already_syncing", "source_id": "pasted-notes-bg4"}
    settled = _wait_until_not_syncing(client, "pasted-notes-bg4")
    assert settled["sync_error"] is None


def test_sync_source_with_wait_false_rejects_an_unknown_source(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    response = client.post("/sync/does-not-exist", json={"wait": False})
    assert response.status_code == 404


def test_sync_source_with_wait_false_accepts_a_source_known_only_to_the_state_registry(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    """Regression: a source registered at runtime (quick-add) lives in the
    state registry immediately, but only lands in *this process's*
    `config.sources` list. A second process reading the same `/state` —
    the sync worker, or (as happened live) the API server after a
    container restart — starts with a `config.sources` that has never
    heard of it; only `SyncEngine._source`'s state-registry fallback makes
    it syncable there. The `wait=false` validation must check the same
    place `_source` ultimately does, not just `config.sources`, or a
    perfectly good source 404s the moment a fresh process is asked to
    sync it in the background."""

    external = tmp_path / "pasted-notes-bg3"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nEven more content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    first_process = TestClient(create_app(config=loaded_config, config_path=config_path))
    first_process.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    # A fresh config load, same `/state` on disk — a new process's starting
    # point, with an empty `config.sources` for anything not in the YAML.
    fresh_config = load_config(config_path)
    assert not any(s.name == "pasted-notes-bg3" for s in fresh_config.sources)
    second_process = TestClient(create_app(config=fresh_config, config_path=config_path))

    response = second_process.post("/sync/pasted-notes-bg3", json={"wait": False})

    assert response.status_code == 200, response.json()
    settled = _wait_until_not_syncing(second_process, "pasted-notes-bg3")
    assert settled["sync_error"] is None


def test_overview_reports_whether_there_is_a_graph_to_show(loaded_config) -> None:
    """The lone-star-node case: a fresh KB must report has_content False."""
    app = create_app(config=loaded_config)
    client = TestClient(app)

    empty = client.get("/overview").json()
    assert empty["has_content"] is False
    assert empty["indexed_artifacts"] == 0

    app.state.engine.sync_source("architecture-notes", "full")
    filled = client.get("/overview").json()
    assert filled["has_content"] is True
    assert filled["indexed_artifacts"] > 0
    assert filled["node_counts"]["knowledge_base"] == 1


def test_graph_route_filters_noisy_node_types(loaded_config) -> None:
    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    client = TestClient(app)

    everything = client.get("/graph").json()
    trimmed = client.get("/graph", params={"exclude_types": "concept,chunk"}).json()

    assert len(trimmed["nodes"]) < len(everything["nodes"])
    assert not any(n.get("type") in ("concept", "chunk") for n in trimmed["nodes"])
    # Totals still describe the whole graph, so the UI can say "x of y".
    assert trimmed["total_nodes"] == everything["total_nodes"]
    assert trimmed["filtered"] is True


def test_mcp_info_exposes_the_tool_surface(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    info = client.get("/mcp/info").json()

    names = {tool["name"] for tool in info["tools"]}
    assert {"search_context", "sync_source", "get_graph_neighbors"} <= names
    assert info["stdio_command"][:2] == ["pheasant", "mcp"]
