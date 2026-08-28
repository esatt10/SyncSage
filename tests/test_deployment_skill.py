import yaml

from tests.conftest import REPO_ROOT

SKILL = REPO_ROOT / ".agents" / "skills" / "pheasant-deploy" / "SKILL.md"
OPENAI_METADATA = SKILL.parent / "agents" / "openai.yaml"


def test_repository_deployment_skill_routes_blank_and_preset_workflows() -> None:
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert frontmatter["name"] == "pheasant-deploy"
    assert "Blank canvas" in text
    assert "Preset" in text
    assert "Never hand-write `pheasant.yaml`" in text
    assert "deploy/compose/docker-compose.advanced.yml" in text
    assert "deploy/compose/docker-compose.scale.yml" in text
    assert "memory_write" in text


def test_deployment_skill_has_discoverable_ui_metadata() -> None:
    metadata = yaml.safe_load(OPENAI_METADATA.read_text(encoding="utf-8"))
    assert metadata["interface"]["display_name"] == "Pheasant Deploy"
    assert "$pheasant-deploy" in metadata["interface"]["default_prompt"]
