from __future__ import annotations

from pathlib import Path

from syncsage.deployment.compose_env import load_compose_environment, render_env_file


def test_compose_env_uses_selected_yaml_for_config_mount(tmp_path: Path) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text(
        """
deployment:
  compose:
    image_repository: ghcr.io/example/syncsage
    image_tag: 1.2.3
    workspace_path: ../workspace with spaces
    vault_path: ../vault
syncsage:
  name: test
sources: []
""".lstrip(),
        encoding="utf-8",
    )

    values = load_compose_environment(config)

    assert values["SYNCSAGE_IMAGE"] == "ghcr.io/example/syncsage:1.2.3"
    assert values["SYNCSAGE_CONFIG_PATH"] == str(config)
    assert values["SYNCSAGE_WORKSPACE_PATH"] == "../workspace with spaces"
    assert values["SYNCSAGE_VAULT_PATH"] == "../vault"

    rendered = render_env_file(values)
    assert 'SYNCSAGE_WORKSPACE_PATH="../workspace with spaces"' in rendered

