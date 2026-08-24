"""Contract tests for the one-command, UI-native Docker reset path."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.fresh.yml"
ENTRYPOINT_PATH = ROOT / "docker-entrypoint.sh"


def test_fresh_ui_compose_is_standalone_and_volume_backed() -> None:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = raw["services"]["pheasant"]

    assert service["build"] == {"context": "."}
    assert service["container_name"] == "${PHEASANT_CONTAINER_NAME:-pheasant}"
    assert service["ports"] == ["${PHEASANT_BIND:-127.0.0.1}:${PHEASANT_PORT:-8765}:8765"]
    assert service["environment"]["PHEASANT_CONFIG"] == "/config/pheasant.yaml"
    assert service["environment"]["PHEASANT_WORKSPACE"] == "/ui-managed-sources"
    assert service["volumes"] == [
        "pheasant-config:/config",
        "pheasant-state:/state",
        "pheasant-exports:/exports",
    ]
    assert set(raw["volumes"]) == {
        "pheasant-config",
        "pheasant-state",
        "pheasant-exports",
    }


def test_fresh_ui_compose_resets_only_its_mounted_pheasant_volumes() -> None:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = raw["services"]["pheasant"]
    script = (ROOT / "docker-fresh-entrypoint.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert service["entrypoint"] == ["/app/docker-fresh-entrypoint.sh"]
    assert "COPY docker-fresh-entrypoint.sh /app/docker-fresh-entrypoint.sh" in dockerfile
    assert "find /config -mindepth 1 -delete" in script
    assert "find /state -mindepth 1 -delete" in script
    assert "find /exports -mindepth 1 -delete" in script
    assert "exec /app/docker-entrypoint.sh serve" in script
    assert "/workspace" not in script
    assert "docker-compose.override.yml" not in script


def _entrypoint_answers(shell_var: str) -> dict:
    """One of the answers JSON blobs the container feeds `pheasant setup`,
    parsed. `shell_var` is the shell variable name in docker-entrypoint.sh."""
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(shell_var)}='(.*)'$", script, re.M)
    assert match, f"docker-entrypoint.sh no longer defines {shell_var}"
    return json.loads(match.group(1))


def _entrypoint_wasm_answers() -> dict:
    """The answers JSON a container with wasmtime installed feeds `pheasant
    setup` — WASM acceleration on, alongside the security audit finding H4
    fix (server.host: 0.0.0.0, unconditional — see the other answers blob
    below) that every container needs regardless of wasmtime."""
    return _entrypoint_answers("SETUP_ANSWERS_WITH_WASM")


def test_a_fresh_container_generates_a_config_with_wasm_acceleration_on() -> None:
    """The image always installs the [wasm] extra, so a container that writes
    its own config should use what it was built with.

    Driven through the **real wizard** rather than asserting on the script's
    text: the flags are schema keys, and a rename would leave a
    string-matching test green while the container silently generated a config
    with acceleration off — which is exactly the failure mode, since both
    accelerators fall back to pure Python without ever erroring. Same reasoning
    covers `server.host` (security audit finding H4): a wrong or missing
    answer would not error either, it would just silently bind loopback-only
    inside a container whose only reachability is that bind.
    """
    from pheasant.setup_wizard import Wizard

    wizard = Wizard(accept_defaults=True, preset=_entrypoint_wasm_answers())
    wizard.run()
    rendered = wizard.config_dict()

    assert rendered["search"]["wasm_relationship_search"] is True
    assert rendered["graph"]["wasm_cross_source_resolution"] is True
    assert rendered["server"]["host"] == "0.0.0.0"


def test_a_fresh_container_without_wasmtime_still_gets_the_host_answer() -> None:
    """security audit finding H4: `server.host: 0.0.0.0` must reach every
    container's generated config, not only ones with wasmtime installed —
    it is a reachability fix, not an acceleration one, and must not be
    accidentally gated behind the same `import wasmtime` check."""
    from pheasant.setup_wizard import Wizard

    answers = _entrypoint_answers("SETUP_ANSWERS_BASE")
    assert answers == {"server.host": "0.0.0.0"}

    wizard = Wizard(accept_defaults=True, preset=answers)
    wizard.run()
    rendered = wizard.config_dict()

    assert rendered["server"]["host"] == "0.0.0.0"
    assert rendered.get("search", {}).get("wasm_relationship_search", False) is False
    assert rendered.get("graph", {}).get("wasm_cross_source_resolution", False) is False


def test_the_default_config_without_those_answers_binds_loopback_only() -> None:
    """The WASM flags are opt-in in the schema and stay that way — the
    container turning them on is a container decision, not a changed
    default. `server.host` is the opposite direction (security audit
    finding H4): the *schema* default is now the safe one (127.0.0.1), and
    it is the container's `SETUP_ANSWERS_BASE`/`_WITH_WASM` answer that
    widens it to 0.0.0.0 — so with no answers at all, the wizard must still
    produce the safe bind, matching a bare `pip install`."""
    from pheasant.setup_wizard import Wizard

    wizard = Wizard(accept_defaults=True)
    wizard.run()
    rendered = wizard.config_dict()

    assert rendered.get("search", {}).get("wasm_relationship_search", False) is False
    assert rendered.get("graph", {}).get("wasm_cross_source_resolution", False) is False
    assert rendered["server"]["host"] == "127.0.0.1"


def test_the_entrypoint_gates_wasm_on_the_extra_being_installed() -> None:
    """Enabling it without wasmtime would still be *correct* — both accelerators
    fall back — but it would log a warning per call and mean nothing."""
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    assert 'python -c "import wasmtime"' in script
    assert "--answers" in script


def test_the_sandboxed_pdf_extractor_is_not_swept_in_with_the_accelerators() -> None:
    """It is a fidelity trade, not an acceleration one.

    The default (`auto`, which prefers pymupdf/python-docx) reads encrypted
    PDFs, LZW/CCITT and Type0/CID CMaps that the sandboxed tokenizer does not,
    so a fresh container must not silently downgrade extraction while claiming
    only to have turned on acceleration.
    """
    answers = _entrypoint_wasm_answers()

    assert set(answers) == {
        "server.host",
        "search.wasm_relationship_search",
        "graph.wasm_cross_source_resolution",
    }
    assert answers["search.wasm_relationship_search"] is True
    assert answers["graph.wasm_cross_source_resolution"] is True


def test_fresh_ui_one_liner_is_documented() -> None:
    command = "docker compose -f docker-compose.fresh.yml up -d --build --force-recreate"
    # Documented where someone looks for it — the how-to guide — and in the
    # compose file's own header. The README is the product front door and
    # deliberately does not carry every operational one-liner.
    assert command in COMPOSE_PATH.read_text(encoding="utf-8")
    assert command in (ROOT / "docs/how-to/run-the-ui.md").read_text(encoding="utf-8")
