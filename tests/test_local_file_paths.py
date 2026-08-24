"""Regression tests: connecting to local files (path handling + diagnostics).

These cover the fixes for "problems connecting to local files if they are not
directly in the subdirectory of the container":

- a relative filesystem source path anchors to ``workspace_root`` (not the
  process CWD, which is ``/app`` in the container);
- a filesystem source whose root is absent (typically: not mounted) is reported
  as a first-class ``path_missing`` sync status instead of a silent 0-file sync;
- the allowlist rejection message is actionable (mount / allowlist hint);
- ``/fs/list`` keeps a configured-but-unmounted browse root (flagged
  ``mounted: false``) instead of silently hiding it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pheasant.api.app import _configured_roots
from pheasant.config.schema import PheasantConfig
from pheasant.security.path_policy import PathPolicyError, resolve_under
from pheasant.sync.engine import SyncEngine


def _base_config(tmp_path: Path, sources: list[dict]) -> dict:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "pheasant": {
            "name": "path-tests",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / "exports"),
        },
        "sources": sources,
    }


def test_relative_filesystem_source_anchors_to_workspace_root(tmp_path: Path) -> None:
    config = PheasantConfig.model_validate(
        _base_config(tmp_path, [{"name": "docs", "type": "document_folder", "path": "docs"}])
    )
    source = config.sources[0]
    assert source.path == (tmp_path / "workspace" / "docs")
    assert source.path.is_absolute()


def test_absolute_filesystem_source_path_is_unchanged(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "docs"
    config = PheasantConfig.model_validate(
        _base_config(tmp_path, [{"name": "docs", "type": "document_folder", "path": str(absolute)}])
    )
    assert config.sources[0].path == absolute


def test_relative_web_source_path_is_not_anchored(tmp_path: Path) -> None:
    # URL/connector-backed types don't read a local path; leave it verbatim.
    config = PheasantConfig.model_validate(
        _base_config(tmp_path, [{"name": "web", "type": "web_collection", "path": "placeholder"}])
    )
    assert config.sources[0].path == Path("placeholder")


def test_missing_filesystem_root_reports_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "not-mounted"  # never created
    config = PheasantConfig.model_validate(
        _base_config(tmp_path, [{"name": "ghost", "type": "document_folder", "path": str(missing)}])
    )
    engine = SyncEngine(config)
    result = engine.sync_source("ghost", "incremental")
    assert result.status == "path_missing"
    assert result.indexed_artifacts == 0
    assert "does not exist" in result.details["error"]
    assert "mounted" in result.details["error"].lower()


def test_resolve_under_rejection_is_actionable() -> None:
    try:
        resolve_under("/etc/passwd", ["/workspace", "/vault"])
    except PathPolicyError as exc:
        message = str(exc)
        assert "allow_workspace_roots" in message
        assert "mount" in message.lower()
    else:  # pragma: no cover - the call must raise
        raise AssertionError("resolve_under accepted a path outside the allowlist")


# `allow_workspace_roots` holds *container* paths, which are POSIX by
# definition (pheasant runs in Docker). On Windows Path("/data") correctly
# resolves to C:/data, so it is the assertion that is POSIX-only, not the code.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only; see comment above.")
def test_configured_roots_keeps_unmounted_allowlisted_root(tmp_path: Path) -> None:
    config = PheasantConfig.model_validate(
        {
            **_base_config(tmp_path, []),
            "security": {
                "allow_user_selected_source_paths": False,
                "allow_workspace_roots": ["/workspace", "/vault", "/data-not-mounted"],
            },
        }
    )
    roots = _configured_roots(config)
    assert Path("/data-not-mounted") in roots
    assert not Path("/data-not-mounted").exists()


def test_resolve_under_refuses_an_empty_root_list() -> None:
    """Security audit finding M4: `resolve_under([])` used to return the
    candidate path unchanged — "no roots" silently meant "no restriction".
    An operator who locks indexing down to an explicit allow-list and then
    mistypes or forgets to mount every one of its entries must end up with
    *nothing* reachable, not *everything*."""

    with pytest.raises(PathPolicyError, match="no roots are configured"):
        resolve_under("/etc/passwd", [])


def test_configured_roots_never_produces_an_empty_list_the_caller_did_not_ask_for(
    tmp_path: Path,
) -> None:
    """The fail-open bug this closes: `_allowed_roots` (now removed) dropped
    a configured-but-unmounted root before handing the list to
    `resolve_under`, so an operator who correctly locked indexing down with
    `allow_user_selected_source_paths: false` and a single, not-yet-mounted
    `allow_workspace_roots` entry got an *empty* effective allow-list — and
    `resolve_under` used to treat that as unrestricted. `_configured_roots`
    (the one surviving list, security audit finding M4) never drops an
    entry for non-existence, so this same config now still has a
    non-empty, correctly-restrictive root list."""

    config = PheasantConfig.model_validate(
        {
            **_base_config(tmp_path, []),
            "pheasant": {
                "name": "path-tests",
                "state_path": str(tmp_path / "state"),
                # A workspace_root that does not exist — the scenario that
                # used to empty the whole list.
                "workspace_root": str(tmp_path / "not-mounted-workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "security": {
                "allow_user_selected_source_paths": False,
                "allow_workspace_roots": ["/also-not-mounted"],
            },
        }
    )

    roots = _configured_roots(config)

    assert roots  # never empty for this config — both entries are kept
    assert (tmp_path / "not-mounted-workspace").resolve() in roots
    assert Path("/also-not-mounted") in roots
    # And the policy check itself still correctly denies an unrelated path.
    with pytest.raises(PathPolicyError):
        resolve_under("/etc/passwd", roots)


# Same as above: the allowlist is in-container POSIX roots, which a native
# Windows run resolves against the current drive.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only; see comment above.")
def test_fs_list_flags_unmounted_root(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config = PheasantConfig.model_validate(
        {
            **_base_config(tmp_path, []),
            "security": {
                "allow_user_selected_source_paths": False,
                "allow_workspace_roots": [
                    str(tmp_path / "workspace"),
                    "/data-not-mounted",
                ],
            },
        }
    )
    client = TestClient(create_app(config=config, config_path=tmp_path / "cfg.yaml"))
    body = client.get("/fs/list").json()
    by_path = {e["path"]: e for e in body["entries"]}
    assert "/data-not-mounted" in by_path
    assert by_path["/data-not-mounted"]["mounted"] is False
    assert by_path[str((tmp_path / "workspace").resolve())]["mounted"] is True
