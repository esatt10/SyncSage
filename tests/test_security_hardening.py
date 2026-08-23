"""Regression gate for the security scan of 2026-07-31, extended 2026-08-23.

Every test here pins a vulnerability that was reachable on `main` and is
now closed. They are written adversarially — each one fails if the guard is
removed — and paired with a "still works" assertion so a future tightening
does not quietly amputate the feature it protects.

The 2026-07-31 findings, in the order they appear below:

1. ``config_path`` on the promote surfaces was written verbatim, turning
   source management into an arbitrary file write (HTTP + MCP).
2. The web/API connectors handed any URL to ``urlopen``, so a
   ``file://`` "web collection" read and indexed the host filesystem.
3. CORS was ``*`` on an unauthenticated API: any page in the user's
   browser could drive every route.
4. ``/relevant-files`` and the raw-content endpoints skipped the Step-32
   ACL filter that ``/search`` applies.
5. Clone URLs reached ``git clone`` argv unvalidated (option injection,
   transport helpers).
6. Backup extraction trusted symlink members.
7. ``max_results`` was unbounded.

The 2026-08-23 findings (see ``docs/security-audit-2026-08-23.md``) are
appended below in the order remediated, each in its own numbered section
starting at 8.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.persistence.backup import _safe_extract
from pheasant.search.hybrid import MAX_RESULTS_CEILING
from pheasant.security.path_policy import PathPolicyError, resolve_config_write_target
from pheasant.sync.connectors import is_fetchable_url, require_fetchable_url
from pheasant.targets import TargetError, validate_clone_url

NOTE = "# Alpha\nthe quick brown widget service runs on port 9000\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "ws" / "notes").mkdir(parents=True)
    (tmp_path / "ws" / "notes" / "a.md").write_text(NOTE, encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "pheasant.yaml").write_text("pheasant:\n  name: sec\n", encoding="utf-8")
    return tmp_path


def _build(workspace: Path, **security: object) -> tuple[PheasantConfig, TestClient]:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "security": security,
            "sources": [],
        }
    )
    client = TestClient(create_app(config, config_path=str(workspace / "pheasant.yaml")))
    return config, client


def _register_notes(client: TestClient, workspace: Path, name: str = "n1") -> None:
    response = client.post(
        "/sources",
        json={
            "name": name,
            "type": "document_folder",
            "path": str(workspace / "ws" / "notes"),
            "sync_now": True,
        },
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 1. Arbitrary file write via promote
# ---------------------------------------------------------------------------


def test_promote_refuses_to_write_outside_allowed_roots(workspace: Path) -> None:
    """``config_path`` must not be able to drop a file anywhere on disk."""

    _config, client = _build(workspace)
    _register_notes(client, workspace)
    victim = workspace / "pwned.yaml"

    response = client.post(
        "/sources/n1/promote",
        json={"config_path": str(victim), "write": True},
    )

    assert response.status_code == 400
    assert not victim.exists()


def test_promote_write_target_ignores_the_source_path_escape_hatch(workspace: Path) -> None:
    """``allow_user_selected_source_paths`` widens *indexing*, not config writes.

    It defaults to true and widens the source-path allowlist to ``/``. If the
    promote guard consulted the same list, the guard would be a no-op on a
    default install — which is exactly how the original bug survived.
    """

    _config, client = _build(workspace, allow_user_selected_source_paths=True)
    _register_notes(client, workspace)
    victim = workspace / "pwned-despite-open-source-paths.yaml"

    response = client.post(
        "/sources/n1/promote",
        json={"config_path": str(victim), "write": True},
    )

    assert response.status_code == 400
    assert not victim.exists()


def test_promote_still_writes_the_servers_own_config(workspace: Path) -> None:
    """The feature itself is intact: no ``config_path`` promotes in place."""

    _config, client = _build(workspace)
    _register_notes(client, workspace)

    response = client.post("/sources/n1/promote", json={"write": True})

    assert response.status_code == 200
    assert response.json()["wrote_config"] is True
    assert "n1" in (workspace / "pheasant.yaml").read_text(encoding="utf-8")


def test_promote_still_writes_under_an_allowed_root(workspace: Path) -> None:
    _config, client = _build(workspace)
    _register_notes(client, workspace)
    target = workspace / "ws" / "alt.yaml"

    response = client.post(
        "/sources/n1/promote",
        json={"config_path": str(target), "write": True},
    )

    assert response.status_code == 200
    assert target.exists()


def test_resolve_config_write_target_defaults_to_the_server_config(tmp_path: Path) -> None:
    server = tmp_path / "pheasant.yaml"
    server.write_text("pheasant: {}\n", encoding="utf-8")

    assert resolve_config_write_target(None, server_config_path=server) == server.resolve()
    assert resolve_config_write_target("", server_config_path=server) == server.resolve()
    with pytest.raises(PathPolicyError):
        resolve_config_write_target(tmp_path / "elsewhere.yaml", server_config_path=server)


def test_mcp_promote_refuses_a_path_outside_allowed_roots(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP facade carries the same guard as HTTP — agents are callers too."""

    from pheasant.mcp_server.tools import PheasantTools

    monkeypatch.setenv("PHEASANT_CONFIG", str(workspace / "pheasant.yaml"))
    config, _client = _build(workspace)
    tools = PheasantTools(config)
    tools.register_source(
        config.knowledge_base_id,
        "n1",
        "document_folder",
        str(workspace / "ws" / "notes"),
    )
    victim = workspace / "mcp-pwned.yaml"

    with pytest.raises(PathPolicyError):
        tools.promote_runtime_source_to_config(
            config.knowledge_base_id, "n1", config_path=str(victim), write=True
        )
    assert not victim.exists()


# ---------------------------------------------------------------------------
# 2. file:// local-file read through the web connector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file:///proc/self/environ",
        "ftp://example.com/secret",
        "/etc/passwd",
    ],
)
def test_connectors_refuse_non_http_urls(url: str) -> None:
    assert not is_fetchable_url(url)
    with pytest.raises(Exception, match="refusing to fetch"):
        require_fetchable_url(url)


@pytest.mark.parametrize("url", ["http://example.com/a", "https://example.com/a"])
def test_connectors_still_fetch_http(url: str) -> None:
    assert is_fetchable_url(url)
    assert require_fetchable_url(url) == url


def test_file_url_web_collection_indexes_nothing(workspace: Path) -> None:
    """End to end: a file:// URL must never reach the index or search."""

    secret = workspace / "secret.txt"
    secret.write_text("TOP-SECRET-API-KEY-abc123", encoding="utf-8")
    _config, client = _build(workspace)

    response = client.post(
        "/sources",
        json={
            "name": "exfil",
            "type": "web_collection",
            "path": str(workspace / "ws"),
            "urls": [f"file://{secret}"],
            "include": ["**/*"],
            "connector": {"allow_experimental": True},
            "sync_now": True,
        },
    )

    # The bad URL is skipped, not fatal: the rest of a collection still syncs.
    assert response.status_code == 200, response.text
    assert response.json()["sync_result"]["indexed_artifacts"] == 0

    hits = client.post("/search", json={"query": "TOP-SECRET-API-KEY-abc123"}).json()["results"]
    assert not any("TOP-SECRET" in str(hit) for hit in hits)


# ---------------------------------------------------------------------------
# 3. CORS on an unauthenticated API
# ---------------------------------------------------------------------------


def test_cors_does_not_admit_arbitrary_origins(workspace: Path) -> None:
    _config, client = _build(workspace)

    response = client.get("/overview", headers={"Origin": "https://evil.example"})

    assert response.headers.get("access-control-allow-origin") not in {"*", "https://evil.example"}


def test_cors_admits_the_configured_ui_origin(workspace: Path) -> None:
    _config, client = _build(workspace)

    response = client.get("/overview", headers={"Origin": "http://localhost:5173"})

    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_wildcard_remains_available_as_an_explicit_opt_in(workspace: Path) -> None:
    """Deployments behind their own authenticating ingress can still opt in."""

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "server": {"api": {"cors_allow_all_origins": True}},
            "sources": [],
        }
    )
    client = TestClient(create_app(config, config_path=str(workspace / "pheasant.yaml")))

    response = client.get("/overview", headers={"Origin": "https://anywhere.example"})

    assert response.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------------
# 4. ACL enforcement parity across every retrieval surface
# ---------------------------------------------------------------------------


def test_relevant_files_enforces_acls_like_search(workspace: Path) -> None:
    """/relevant-files is /search with a different projection — same filter.

    principal_source: header (required alongside acl_enforced since the
    2026-08-23 C3 fix — see PheasantConfig.model_validate) — the principal
    goes in X-Pheasant-Principal, not the body, and a body-supplied one is
    ignored.
    """

    _config, client = _build(
        workspace, acl_enforced=True, default_visibility="private", principal_source="header"
    )
    _register_notes(client, workspace)

    anonymous = client.post("/relevant-files", json={"query": "widget service"}).json()["files"]
    identified = client.post(
        "/relevant-files",
        json={"query": "widget service"},
        headers={"X-Pheasant-Principal": "user:alice"},
    ).json()["files"]

    assert anonymous == []
    assert identified, "an authorized principal must still get results"


def test_relevant_files_still_returns_files_under_a_small_limit(workspace: Path) -> None:
    """Threading ACLs through must not change *what* this route retrieves.

    /relevant-files projects results down to those carrying a path. Graph
    nodes (concepts, symbols) carry none, so letting graph hits into the
    merge crowds the file hits out and the route answers with an empty list
    — which is what happened the first time ACL enforcement was wired here.
    A workspace with wikilinks generates exactly those competing nodes.
    """

    notes = workspace / "ws" / "notes"
    (notes / "index.md").write_text("# Index\n[[Runbook]] covers deploys.\n", encoding="utf-8")
    (notes / "runbook.md").write_text(
        "# Runbook\nThe widget service listens on port 9000 and is deployed by CI.\n",
        encoding="utf-8",
    )
    _config, client = _build(workspace)
    _register_notes(client, workspace)

    response = client.post("/relevant-files", json={"query": "widget service", "max_results": 3})
    files = response.json()["files"]

    assert files, "a file-bearing query must return files"
    assert all(entry.get("relative_path") for entry in files)


def test_content_endpoints_enforce_acls(workspace: Path) -> None:
    """Filtering search while serving the same bytes elsewhere is not enforcement.

    principal_source: header, matching the other ACL test in this file —
    see its docstring.
    """

    _config, client = _build(
        workspace, acl_enforced=True, default_visibility="private", principal_source="header"
    )
    _register_notes(client, workspace)
    alice = {"X-Pheasant-Principal": "user:alice"}
    files = client.post("/relevant-files", json={"query": "widget service"}, headers=alice).json()[
        "files"
    ]
    node_id = files[0]["node_id"]

    assert client.get("/files/summary", params={"path": "a.md"}).status_code == 403
    assert client.get("/nodes/content", params={"node_id": node_id}).status_code == 403

    allowed_summary = client.get("/files/summary", params={"path": "a.md"}, headers=alice)
    allowed_content = client.get("/nodes/content", params={"node_id": node_id}, headers=alice)
    assert allowed_summary.status_code == 200
    assert allowed_content.status_code == 200


def test_content_endpoints_unchanged_when_enforcement_is_off(workspace: Path) -> None:
    """Default (acl_enforced: false) stays byte-identical to pre-32 behavior."""

    _config, client = _build(workspace)
    _register_notes(client, workspace)

    assert client.get("/files/summary", params={"path": "a.md"}).status_code == 200
    assert client.post("/relevant-files", json={"query": "widget service"}).json()["files"]


# ---------------------------------------------------------------------------
# 5. git clone argument / transport injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c touch% /tmp/pwned% x.git",  # transport helper = RCE
        "--upload-pack=touch /tmp/pwned.git",  # leading dash = git option
        "-c core.pager=sh.git",
        "file:///etc/passwd.git",
        "ftp://example.com/x.git",
        "",
    ],
)
def test_clone_urls_that_are_not_clone_urls_are_refused(url: str) -> None:
    with pytest.raises(TargetError):
        validate_clone_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/me/proj",
        "https://github.com/me/proj.git",
        "http://git.internal/me/proj.git",
        "git@github.com:me/proj.git",
        "ssh://git@host/me/proj.git",
        "git://host/proj.git",
    ],
)
def test_real_clone_urls_still_pass(url: str) -> None:
    assert validate_clone_url(url) == url


def test_git_env_pins_transports_and_never_prompts() -> None:
    from pheasant.targets import _git_env

    env = _git_env()
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert pairs["protocol.allow"] == "never"
    assert pairs["protocol.https.allow"] == "always"


# ---------------------------------------------------------------------------
# 6. Backup archive extraction
# ---------------------------------------------------------------------------


def _tar_with(members: list[tarfile.TarInfo], payloads: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member in members:
            data = payloads.get(member.name)
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return buffer


def test_backup_extraction_rejects_escaping_symlinks(tmp_path: Path) -> None:
    """A symlink member passes a name-only check and escapes on the next write."""

    link = tarfile.TarInfo("escape")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc"
    buffer = _tar_with([link], {})

    with tarfile.open(fileobj=buffer, mode="r") as tar, pytest.raises(ValueError):
        _safe_extract(tar, tmp_path / "dest")


def test_backup_extraction_rejects_traversing_members(tmp_path: Path) -> None:
    member = tarfile.TarInfo("../escaped.txt")
    member.size = 3
    buffer = _tar_with([member], {"../escaped.txt": b"bad"})

    with tarfile.open(fileobj=buffer, mode="r") as tar, pytest.raises(ValueError):
        _safe_extract(tar, tmp_path / "dest")


def test_backup_extraction_still_restores_ordinary_members(tmp_path: Path) -> None:
    member = tarfile.TarInfo("graph.json")
    member.size = 2
    buffer = _tar_with([member], {"graph.json": b"{}"})
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(fileobj=buffer, mode="r") as tar:
        _safe_extract(tar, dest)

    assert (dest / "graph.json").read_bytes() == b"{}"


# ---------------------------------------------------------------------------
# 7. Unbounded result counts
# ---------------------------------------------------------------------------


def test_max_results_is_clamped(workspace: Path) -> None:
    _config, client = _build(workspace)
    _register_notes(client, workspace)

    response = client.post("/search", json={"query": "widget", "max_results": 10**9})

    assert response.status_code == 200
    assert len(response.json()["results"]) <= MAX_RESULTS_CEILING


# ---------------------------------------------------------------------------
# 8. WASM AOT cache deserialized from a shared, attacker-writable temp dir
#    (docs/security-audit-2026-08-23.md finding C5)
# ---------------------------------------------------------------------------


def test_wasm_cache_dir_owned_by_another_uid_is_never_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache directory this process did not create is refused, not trusted.

    Simulates "another uid pre-created the shared cache dir" the only way a
    single-uid test process can: by making the ownership check itself see a
    foreign uid. A real deserialize_file call is not exercised here (that
    needs wasmtime and is covered by test_wasm_accel_loader.py's fixtures);
    this isolates the trust decision the security fix is actually about.
    """
    from pheasant.sandbox.accel import cache_security

    cache_dir = tmp_path / "wasm-cache"
    cache_dir.mkdir(mode=0o700)
    real_uid = cache_security.os.getuid()
    monkeypatch.setattr(cache_security.os, "getuid", lambda: real_uid + 1)

    assert cache_security.secure_dir(cache_dir) is None


def test_wasm_cache_dir_that_is_group_or_other_accessible_is_refused(tmp_path: Path) -> None:
    from pheasant.sandbox.accel.cache_security import secure_dir

    cache_dir = tmp_path / "wasm-cache"
    cache_dir.mkdir(mode=0o755)  # group/other readable+executable

    assert secure_dir(cache_dir) is None


def test_wasm_cache_dir_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    from pheasant.sandbox.accel.cache_security import secure_dir

    real = tmp_path / "real-cache"
    real.mkdir(mode=0o700)
    link = tmp_path / "wasm-cache-link"
    link.symlink_to(real)

    assert secure_dir(link) is None


def test_wasm_cache_file_owned_by_another_uid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.sandbox.accel import cache_security

    cache_file = tmp_path / "accel-deadbeef.cwasm"
    cache_file.write_bytes(b"not really a compiled module")
    real_uid = cache_security.os.getuid()
    monkeypatch.setattr(cache_security.os, "getuid", lambda: real_uid + 1)

    assert cache_security.secure_cache_file(cache_file) is None


def test_wasm_cache_dir_and_file_created_by_this_process_are_still_trusted(
    tmp_path: Path,
) -> None:
    """The "still works" half: a private cache this process itself created
    and wrote is accepted, so the hardening does not amputate the cache."""
    from pheasant.sandbox.accel.cache_security import secure_cache_file, secure_dir

    cache_dir = tmp_path / "wasm-cache"
    assert secure_dir(cache_dir) == cache_dir
    assert cache_dir.stat().st_mode & 0o777 == 0o700

    cache_file = cache_dir / "accel-deadbeef.cwasm"
    cache_file.write_bytes(b"a legitimate serialized module")
    assert secure_cache_file(cache_file) == cache_file

    # A file that does not exist yet is not distrusted — there is nothing to
    # distrust, and refusing it would break the first-ever cache write.
    assert secure_cache_file(cache_dir / "not-written-yet.cwasm") is not None


# ---------------------------------------------------------------------------
# 9. Arbitrary environment-variable exfiltration via api_key_env, and
#    unrestricted SSRF (docs/security-audit-2026-08-23.md findings C1, C2)
# ---------------------------------------------------------------------------


def test_embeddings_update_refuses_an_unapproved_credential_env(workspace: Path) -> None:
    """`api_key_env` may not name an arbitrary environment variable."""

    _config, client = _build(workspace)

    response = client.put("/search/embeddings", json={"api_key_env": "AWS_SECRET_ACCESS_KEY"})

    assert response.status_code == 400
    assert "AWS_SECRET_ACCESS_KEY" in response.json()["detail"]
    assert "not an approved credential" in response.json()["detail"]


def test_embeddings_update_still_accepts_the_providers_own_default_env(workspace: Path) -> None:
    config, client = _build(workspace)

    response = client.put("/search/embeddings", json={"api_key_env": "OPENAI_API_KEY"})

    assert response.status_code == 200
    assert config.search.embeddings.api_key_env == "OPENAI_API_KEY"


def test_config_section_patch_refuses_a_nested_unapproved_credential_env(
    workspace: Path,
) -> None:
    """The generic PATCH surface is checked too, at any nesting depth."""

    _config, client = _build(workspace)

    response = client.patch(
        "/config/section/assistant",
        json={"values": {"api_key_env": "PHEASANT_INDEX_WORKER_TOKEN"}},
    )

    assert response.status_code == 400
    assert "PHEASANT_INDEX_WORKER_TOKEN" in response.json()["detail"]


def test_config_section_patch_still_accepts_a_known_provider_env(workspace: Path) -> None:
    config, client = _build(workspace)

    response = client.patch(
        "/config/section/assistant",
        json={"values": {"api_key_env": "ANTHROPIC_API_KEY"}},
    )

    assert response.status_code == 200
    assert config.assistant.api_key_env == "ANTHROPIC_API_KEY"


def test_register_source_refuses_an_unapproved_credential_env_for_a_first_party_connector(
    workspace: Path,
) -> None:
    _config, client = _build(workspace)

    response = client.post(
        "/sources",
        json={
            "name": "notion-docs",
            "type": "notion",
            "path": "",
            "connector": {"api_key_env": "PHEASANT_INDEX_WORKER_TOKEN"},
        },
    )

    assert response.status_code == 400
    assert "PHEASANT_INDEX_WORKER_TOKEN" in response.json()["detail"]


def test_register_source_still_accepts_the_connectors_own_default_env(workspace: Path) -> None:
    _config, client = _build(workspace)

    response = client.post(
        "/sources",
        json={
            "name": "notion-docs",
            "type": "notion",
            "path": "",
            "connector": {"api_key_env": "NOTION_TOKEN"},
        },
    )

    assert response.status_code == 200, response.text


def test_check_fetchable_blocks_literal_metadata_and_private_addresses() -> None:
    from pheasant.security.egress import EgressBlocked, check_fetchable

    for url in (
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8080/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://localhost/",
        "http://[::1]/",
        "ftp://169.254.169.254/",
    ):
        with pytest.raises(EgressBlocked):
            check_fetchable(url)


def test_check_fetchable_allows_private_addresses_when_opted_in() -> None:
    from pheasant.security.egress import check_fetchable

    assert check_fetchable("http://169.254.169.254/", allow_private=True) == (
        "http://169.254.169.254/"
    )
    assert check_fetchable("http://127.0.0.1:11434/api/generate", allow_private=True)


def test_check_fetchable_does_not_require_dns_to_be_reachable() -> None:
    """A hostname that fails to resolve is not treated as a security denial —
    see check_fetchable's docstring for why coupling the check to live DNS
    would be a worse trade than it looks."""

    from pheasant.security.egress import check_fetchable

    # This name is guaranteed never to resolve (RFC 2606).
    assert check_fetchable("https://name.invalid/path") == "https://name.invalid/path"


def test_open_url_rejects_a_redirect_to_a_blocked_destination() -> None:
    """The bypass this closes: an initially-clean URL redirects to a
    scheme/host the initial check would have refused, and plain `urlopen`
    follows it with no further check."""

    from urllib.request import Request

    from pheasant.security.egress import EgressBlocked, _ValidatingRedirectHandler

    handler = _ValidatingRedirectHandler(allow_private=False)
    request = Request("http://example.test/start")

    with pytest.raises(EgressBlocked):
        handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1:9999/secret")


def test_resolve_credential_env_rejects_names_outside_the_allowlist() -> None:
    from pheasant.security.credentials import CredentialEnvNotAllowed, resolve_credential_env

    with pytest.raises(CredentialEnvNotAllowed):
        resolve_credential_env("AWS_SECRET_ACCESS_KEY", allowed={"OPENAI_API_KEY"})


def test_resolve_credential_env_allows_none_and_allowlisted_names() -> None:
    from pheasant.security.credentials import resolve_credential_env

    assert resolve_credential_env(None, allowed=set()) is None
    assert resolve_credential_env("", allowed=set()) == ""
    assert resolve_credential_env("OPENAI_API_KEY", allowed={"OPENAI_API_KEY"}) == "OPENAI_API_KEY"


def test_known_credential_envs_covers_every_first_party_default(workspace: Path) -> None:
    from pheasant.security.credentials import known_credential_envs

    config, _client = _build(workspace)
    envs = known_credential_envs(config)

    assert {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "NOTION_TOKEN",
        "GDRIVE_TOKEN",
        "SLACK_TOKEN",
        "CONFLUENCE_TOKEN",
        "IMAP_CREDENTIALS",
        "IDP_TOKEN",
    } <= envs


def test_plugin_connector_types_are_not_credential_checked() -> None:
    """A plugin's own env-var convention cannot be known in advance — see
    CHECKABLE_CONNECTOR_TYPES's docstring. Only the fixed first-party set is
    checked; this pins the boundary so it cannot silently widen or narrow."""

    from pheasant.security.credentials import CHECKABLE_CONNECTOR_TYPES

    assert CHECKABLE_CONNECTOR_TYPES == {
        "notion",
        "gdrive",
        "slack",
        "confluence",
        "imap",
    }


# ---------------------------------------------------------------------------
# 10. Memory-record listing ignored scope isolation
#    (docs/security-audit-2026-08-23.md finding C4)
# ---------------------------------------------------------------------------


def _enable_memory(client: TestClient) -> None:
    response = client.post("/memory/enable", json={})
    assert response.status_code == 200, response.text


def test_memory_list_without_a_principal_still_returns_everything(workspace: Path) -> None:
    """The pre-fix behavior is preserved for standalone/single-user use,
    which never supplies a principal — rule 7, byte-identical default."""

    _config, client = _build(workspace)
    _enable_memory(client)
    client.post(
        "/memory", json={"text": "alice's note", "scope": "user", "principal": "user:alice"}
    )
    client.post("/memory", json={"text": "bob's note", "scope": "user", "principal": "user:bob"})

    response = client.get("/memory")

    assert response.status_code == 200
    texts = {record["text"] for record in response.json()["records"]}
    assert texts == {"alice's note", "bob's note"}


def test_memory_list_with_a_principal_hides_another_principals_records(workspace: Path) -> None:
    _config, client = _build(workspace)
    _enable_memory(client)
    client.post(
        "/memory", json={"text": "alice's note", "scope": "user", "principal": "user:alice"}
    )
    client.post("/memory", json={"text": "bob's note", "scope": "user", "principal": "user:bob"})
    client.post(
        "/memory", json={"text": "shared policy", "scope": "org", "principal": "user:alice"}
    )

    response = client.get("/memory", params={"principal": "user:alice"})

    assert response.status_code == 200
    texts = {record["text"] for record in response.json()["records"]}
    assert texts == {"alice's note", "shared policy"}
    assert "bob's note" not in texts


def test_mcp_memory_list_with_a_principal_hides_another_principals_records(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.mcp_server.tools import PheasantTools

    monkeypatch.setenv("PHEASANT_CONFIG", str(workspace / "pheasant.yaml"))
    config, client = _build(workspace)
    _enable_memory(client)
    client.post(
        "/memory", json={"text": "alice's note", "scope": "user", "principal": "user:alice"}
    )
    client.post("/memory", json={"text": "bob's note", "scope": "user", "principal": "user:bob"})

    tools = PheasantTools(config)
    result = tools.memory_list(config.knowledge_base_id, principal="user:alice")

    texts = {record["text"] for record in result["records"]}
    assert texts == {"alice's note"}

    # The "still works" half: no principal is the resource's own behavior
    # (pheasant://…/memory never passes one) and must stay unfiltered.
    unfiltered = tools.memory_list(config.knowledge_base_id)
    assert {record["text"] for record in unfiltered["records"]} == {
        "alice's note",
        "bob's note",
    }


def test_is_memory_record_visible_matches_the_documented_scope_rules() -> None:
    from pheasant.security.acl import is_memory_record_visible

    # org scope: visible to anyone, including no principal at all.
    assert is_memory_record_visible("org", "user:alice", {"user:alice"}) is True
    assert is_memory_record_visible("org", "user:alice", {"user:bob"}) is True
    assert is_memory_record_visible("org", "user:alice", None) is True

    # user/session scope: only the writer.
    assert is_memory_record_visible("user", "user:alice", {"user:alice"}) is True
    assert is_memory_record_visible("user", "user:alice", {"user:bob"}) is False
    assert is_memory_record_visible("session", "user:alice", None) is False

    # No recorded writer, not org scope: any authenticated principal, never
    # an anonymous one.
    assert is_memory_record_visible("user", None, {"user:carol"}) is True
    assert is_memory_record_visible("user", None, None) is False


# ---------------------------------------------------------------------------
# 11. A worker's id/path/source_id/relative_path/git_branch/git_commit were
#    trusted from the wire (docs/security-audit-2026-08-23.md finding H5)
# ---------------------------------------------------------------------------


def _task(*, source_name: str = "docs", relative_path: str = "guide.md") -> dict:
    return {
        "source": {
            "name": source_name,
            "type": "web_collection",
            "path": "/does/not/matter",
            "chunking": {},
            "taxonomy": {"enabled": False},
        },
        "item": {
            "identity": f"web:{relative_path}",
            "relative_path": relative_path,
            "uri": f"https://example.test/{relative_path}",
            "mime_type": "text/markdown",
            "size_bytes": None,
            "sha256": None,
            "mtime": None,
            "etag": None,
            "metadata": {},
        },
        "payload": {"metadata": {}},
        "git_metadata": None,
    }


def _wire_payload(**overrides: object) -> dict:
    payload = {
        "id": "file:attacker-source:../../etc/passwd:branch=none",
        "source_id": "attacker-source",
        "path": "/etc/passwd",
        "relative_path": "../../etc/passwd",
        "type": "text",
        "mime_type": "text/markdown",
        "size_bytes": 3,
        "sha256": "deadbeef",
        "mtime": "2026-01-01T00:00:00Z",
        "git_branch": "attacker-branch",
        "git_commit": "deadbeef",
        "chunks": [],
    }
    payload.update(overrides)
    return payload


def test_parsed_from_wire_ignores_a_forged_id_and_path() -> None:
    """The worker's `id`/`path` are never trusted — they are derived from
    the coordinator's own task record, which a worker cannot influence."""

    from pheasant.sync.remote_worker import parsed_from_wire

    task = _task(source_name="docs", relative_path="guide.md")
    parsed = parsed_from_wire(_wire_payload(), task)

    assert parsed is not None
    assert parsed.id == "file:docs:guide.md:branch=none"
    assert parsed.source_id == "docs"
    assert parsed.relative_path == "guide.md"
    # None of the forged values leaked through.
    assert "attacker" not in parsed.id
    assert "etc/passwd" not in parsed.path
    assert parsed.git_branch != "attacker-branch"


def test_parsed_from_wire_still_uses_the_wires_content_fields(tmp_path: Path) -> None:
    """The "still works" half: fields that are genuinely the worker's own
    answer (parsed text, sha256, mime type) still come from the wire."""

    from pheasant.sync.remote_worker import parsed_from_wire

    task = _task()
    parsed = parsed_from_wire(
        _wire_payload(sha256="realhash", mime_type="text/plain", size_bytes=42),
        task,
    )

    assert parsed is not None
    assert parsed.sha256 == "realhash"
    assert parsed.mime_type == "text/plain"
    assert parsed.size_bytes == 42


def test_parsed_from_wire_matches_the_local_parsing_grammar() -> None:
    """A legitimate worker's id must equal what local parsing would have
    produced for the same source/item — the whole reason recomputation is
    lossless rather than a behavior change."""

    from pheasant.sync.remote_worker import parsed_from_wire

    task = _task(source_name="my-source", relative_path="a/b/c.md")
    parsed = parsed_from_wire(_wire_payload(), task)

    assert parsed is not None
    assert parsed.id == "file:my-source:a/b/c.md:branch=none"


# ---------------------------------------------------------------------------
# 12. ACL enforcement trusted a self-asserted principal
#    (docs/security-audit-2026-08-23.md finding C3)
# ---------------------------------------------------------------------------


def test_acl_enforced_requires_a_non_body_principal_source(workspace: Path) -> None:
    """`acl_enforced: true` cannot be configured with the unauthenticated
    default principal channel — the exact combination that made the
    feature advisory in every shipped configuration."""

    with pytest.raises(ValueError, match="principal_source"):
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "sec",
                    "workspace_root": str(workspace / "ws"),
                    "state_path": str(workspace / "state"),
                    "exports_path": str(workspace / "exports"),
                },
                "security": {"acl_enforced": True},
            }
        )


def test_acl_enforced_still_loads_with_header_principal_source(workspace: Path) -> None:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "security": {"acl_enforced": True, "principal_source": "header"},
        }
    )
    assert config.security.acl_enforced is True
    assert config.security.principal_source == "header"


def test_unknown_principal_source_is_rejected(workspace: Path) -> None:
    with pytest.raises(ValueError, match="principal_source"):
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "sec",
                    "workspace_root": str(workspace / "ws"),
                    "state_path": str(workspace / "state"),
                    "exports_path": str(workspace / "exports"),
                },
                "security": {"principal_source": "cookie"},
            }
        )


class _FakeSecurity:
    def __init__(self, **kwargs: object) -> None:
        self.principal_source = "body"
        self.principal_header = "X-Pheasant-Principal"
        self.principal_signing_public_key_ref = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeConfig:
    def __init__(self, **kwargs: object) -> None:
        self.security = _FakeSecurity(**kwargs)


def test_resolve_http_principal_body_mode_passes_through_unauthenticated() -> None:
    from pheasant.security.principal import resolve_http_principal

    principal, groups = resolve_http_principal(
        headers={},
        body_principal="user:alice",
        body_groups=["group:eng"],
        config=_FakeConfig(),
    )
    assert principal == "user:alice"
    assert groups == ["group:eng"]


def test_resolve_http_principal_header_mode_ignores_the_body() -> None:
    from pheasant.security.principal import resolve_http_principal

    config = _FakeConfig(principal_source="header")
    principal, groups = resolve_http_principal(
        headers={"X-Pheasant-Principal": "user:carol"},
        body_principal="user:attacker",
        body_groups=["group:admin"],
        config=config,
    )
    assert principal == "user:carol"
    assert groups is None  # groups are never taken from a header


def test_resolve_http_principal_header_mode_with_no_header_is_anonymous() -> None:
    from pheasant.security.principal import resolve_http_principal

    config = _FakeConfig(principal_source="header")
    principal, groups = resolve_http_principal(
        headers={}, body_principal="user:attacker", body_groups=None, config=config
    )
    assert principal is None
    assert groups is None


def test_resolve_http_principal_unknown_mode_is_anonymous_not_body() -> None:
    """A hand-built config (bypassing model_validate) with a bogus mode must
    not silently fall back to trusting the body."""

    from pheasant.security.principal import resolve_http_principal

    config = _FakeConfig(principal_source="carrier-pigeon")
    principal, groups = resolve_http_principal(
        headers={}, body_principal="user:alice", body_groups=None, config=config
    )
    assert principal is None
    assert groups is None


def _signed_assertion(claims: dict, seed: bytes) -> tuple[str, str]:
    import base64
    import json

    from pheasant.synapse.signing import _require_crypto

    _require_crypto()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    assertion_bytes = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    signature = private_key.sign(assertion_bytes)
    return (
        base64.b64encode(assertion_bytes).decode("ascii"),
        base64.b64encode(signature).decode("ascii"),
    )


def test_verify_signed_principal_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("cryptography")
    from pheasant.synapse.signing import public_key_b64, verify_signed_principal

    seed = bytes(range(32))
    monkeypatch.setenv("TEST_ROUTER_PUBKEY", public_key_b64(seed))
    assertion, signature = _signed_assertion({"principal": "user:alice", "groups": ["eng"]}, seed)

    claims = verify_signed_principal(assertion, signature, "TEST_ROUTER_PUBKEY")

    assert claims["principal"] == "user:alice"
    assert claims["groups"] == ["eng"]


def test_verify_signed_principal_rejects_a_tampered_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cryptography")
    from pheasant.synapse.signing import (
        PrincipalSignatureError,
        public_key_b64,
        verify_signed_principal,
    )

    seed = bytes(range(32))
    other_seed = bytes(range(1, 33))
    monkeypatch.setenv("TEST_ROUTER_PUBKEY", public_key_b64(seed))
    # Signed with a *different* key than the one this deployment trusts.
    assertion, signature = _signed_assertion({"principal": "user:mallory"}, other_seed)

    with pytest.raises(PrincipalSignatureError):
        verify_signed_principal(assertion, signature, "TEST_ROUTER_PUBKEY")


def test_verify_signed_principal_rejects_an_expired_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cryptography")
    from pheasant.synapse.signing import (
        PrincipalSignatureError,
        public_key_b64,
        verify_signed_principal,
    )

    seed = bytes(range(32))
    monkeypatch.setenv("TEST_ROUTER_PUBKEY", public_key_b64(seed))
    assertion, signature = _signed_assertion(
        {"principal": "user:alice", "exp": "2020-01-01T00:00:00Z"}, seed
    )

    with pytest.raises(PrincipalSignatureError, match="expired"):
        verify_signed_principal(assertion, signature, "TEST_ROUTER_PUBKEY")


def test_resolve_http_principal_signed_mode_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full chain: a valid router-signed header resolves to its
    principal; a caller-supplied body principal alongside it is ignored."""

    pytest.importorskip("cryptography")
    from pheasant.security.principal import resolve_http_principal
    from pheasant.synapse.signing import public_key_b64

    seed = bytes(range(32))
    monkeypatch.setenv("TEST_ROUTER_PUBKEY", public_key_b64(seed))
    assertion, signature = _signed_assertion({"principal": "user:dana"}, seed)
    config = _FakeConfig(
        principal_source="signed", principal_signing_public_key_ref="TEST_ROUTER_PUBKEY"
    )

    principal, groups = resolve_http_principal(
        headers={
            "X-Pheasant-Principal-Assertion": assertion,
            "X-Pheasant-Principal-Signature": signature,
        },
        body_principal="user:attacker",
        body_groups=None,
        config=config,
    )

    assert principal == "user:dana"


def test_resolve_http_principal_signed_mode_with_no_assertion_is_anonymous() -> None:
    from pheasant.security.principal import resolve_http_principal

    config = _FakeConfig(
        principal_source="signed", principal_signing_public_key_ref="TEST_ROUTER_PUBKEY"
    )
    principal, groups = resolve_http_principal(
        headers={}, body_principal="user:attacker", body_groups=None, config=config
    )
    assert principal is None
