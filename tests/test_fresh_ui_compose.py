"""Contract tests for the one-command, UI-native Docker reset path."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.fresh.yml"


def test_fresh_ui_compose_is_standalone_and_volume_backed() -> None:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = raw["services"]["pheasant"]

    assert service["build"] == {"context": "."}
    assert service["container_name"] == "${PHEASANT_CONTAINER_NAME:-pheasant}"
    assert service["ports"] == [
        "${PHEASANT_BIND:-127.0.0.1}:${PHEASANT_PORT:-8765}:8765"
    ]
    assert service["environment"]["PHEASANT_CONFIG"] == "/config/pheasant.yaml"
    assert service["environment"]["PHEASANT_WORKSPACE"] == "/ui-managed-sources"
    assert service["volumes"] == [
        "pheasant-config:/config",
        "pheasant-state:/state",
        "pheasant-vault:/vault",
        "pheasant-exports:/exports",
    ]
    assert set(raw["volumes"]) == {
        "pheasant-config",
        "pheasant-state",
        "pheasant-vault",
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
    assert "find /vault -mindepth 1 -delete" in script
    assert "find /exports -mindepth 1 -delete" in script
    assert "exec /app/docker-entrypoint.sh serve" in script
    assert "/workspace" not in script
    assert "docker-compose.override.yml" not in script


def test_fresh_ui_one_liner_is_documented() -> None:
    command = "docker compose -f docker-compose.fresh.yml up -d --build --force-recreate"
    assert command in COMPOSE_PATH.read_text(encoding="utf-8")
    assert command in (ROOT / "README.md").read_text(encoding="utf-8")
    assert command in (ROOT / "docs/how-to/run-the-ui.md").read_text(encoding="utf-8")
