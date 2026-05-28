from __future__ import annotations

from pathlib import Path

from syncsage.config.loader import load_layered_config, parse_override_pairs, render_init_config
from tests.conftest import call_cli


def test_layered_config_profile_and_overrides(config_path: Path) -> None:
    cfg = load_layered_config(
        config_path,
        profile="team",
        overrides=parse_override_pairs(["server.port=9999", "obsidian.template_profile=research"]),
    )

    assert cfg.server.port == 9999
    assert cfg.syncsage.environment == "team"
    assert cfg.obsidian.template_profile == "research"
    assert {source.name for source in cfg.sources} == {
        "syncsage-repo",
        "architecture-notes",
        "product-documents",
    }


def test_init_config_rendering_is_profile_aware() -> None:
    rendered = render_init_config("dev")

    assert "SyncSage dev profile configuration" in rendered
    assert "template_profile: engineering" in rendered
    assert "sources: []" in rendered


def test_cli_config_show_init_and_doctor(config_path: Path, tmp_path: Path) -> None:
    init_path = tmp_path / "generated.yaml"

    init_result = call_cli(["init", "--profile", "quickstart", "--output", str(init_path)])
    show_result = call_cli(
        [
            "config",
            "show",
            "--effective",
            "--config",
            str(config_path),
            "--profile",
            "dev",
            "--set",
            "server.port=9001",
        ]
    )
    doctor_result = call_cli(["doctor", "--config", str(config_path), "--profile", "quickstart"])

    assert init_result.returncode == 0, init_result.stderr or init_result.stdout
    assert init_path.exists()
    assert show_result.returncode == 0, show_result.stderr or show_result.stdout
    assert "port: 9001" in show_result.stdout
    assert "syncsage-repo" in show_result.stdout
    assert doctor_result.returncode == 0, doctor_result.stderr or doctor_result.stdout
    assert "Doctor ok" in doctor_result.stdout
