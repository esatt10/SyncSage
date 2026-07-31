"""The `assistant:` config block must actually reach the config object.

Adding a dataclass field is not enough — ``SyncSageConfig.model_validate``
builds each section explicitly, so a section missing from that list is
silently ignored and every setting falls back to its default. This test is
the guard: it round-trips a full non-default block through YAML and back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from syncsage.config.loader import load_config, load_layered_config
from syncsage.config.schema import SyncSageConfig


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "syncsage.yaml"
    config.write_text(body, encoding="utf-8")
    return config


def test_defaults_are_offline_and_permissive(tmp_path: Path) -> None:
    config = SyncSageConfig()
    assert config.assistant.enabled is True
    assert config.assistant.provider == "auto"
    assert config.assistant.allow_session_keys is True
    # No provider pinned and no key baked in — the default deployment is the
    # offline extractive path.
    assert config.assistant.model is None
    assert config.assistant.api_key_env is None


def test_assistant_block_is_loaded_from_yaml(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path,
        "syncsage:\n"
        "  name: kb\n"
        "assistant:\n"
        "  provider: openai\n"
        "  model: my-model\n"
        "  base_url: http://127.0.0.1:9099/v1\n"
        "  allow_session_keys: false\n"
        "  max_context_chunks: 3\n"
        "  session_key_ttl_minutes: 15\n",
    )

    config = load_config(config_path)

    assert config.assistant.provider == "openai"
    assert config.assistant.model == "my-model"
    assert config.assistant.base_url == "http://127.0.0.1:9099/v1"
    assert config.assistant.allow_session_keys is False
    assert config.assistant.max_context_chunks == 3
    assert config.assistant.session_key_ttl_minutes == 15


def test_assistant_block_survives_the_layered_loader(tmp_path: Path) -> None:
    config_path = _write(tmp_path, "assistant:\n  provider: gemini\n  enabled: false\n")

    config = load_layered_config(config_path, "quickstart", {})

    assert config.assistant.provider == "gemini"
    assert config.assistant.enabled is False


def test_assistant_block_round_trips_through_model_dump(tmp_path: Path) -> None:
    original = load_config(_write(tmp_path, "assistant:\n  provider: anthropic\n"))
    dumped = original.model_dump(mode="json")
    assert dumped["assistant"]["provider"] == "anthropic"

    reloaded = SyncSageConfig.model_validate(yaml.safe_load(yaml.safe_dump(dumped)))
    assert reloaded.assistant.provider == "anthropic"


@pytest.mark.parametrize("value", ["3", 3])
def test_scalar_fields_are_coerced(tmp_path: Path, value) -> None:
    """YAML shims can hand back strings where ints are declared."""
    config = SyncSageConfig.model_validate({"assistant": {"max_context_chunks": value}})
    assert config.assistant.max_context_chunks == 3
