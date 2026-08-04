"""Acceptance tests for configuration loading and validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tests.conftest import call_cli, first_attr, import_any, require_attr


def _as_mapping_or_attrs(config: object) -> object:
    return config


def _get(config: object, key: str) -> object:
    if isinstance(config, Mapping):
        return config[key]
    return getattr(config, key)


def _source_names(config: object) -> set[str]:
    sources = _get(_as_mapping_or_attrs(config), "sources")
    names: set[str] = set()
    for source in sources:  # type: ignore[union-attr]
        names.add(str(source["name"] if isinstance(source, Mapping) else source.name))
    return names


def test_config_loads_example_yaml_with_expected_sources(loaded_config: object, state_path: Path, vault_path: Path) -> None:
    """The rendered example config should load and preserve configured paths/sources."""

    assert _source_names(loaded_config) == {
        "syncsage-repo",
        "architecture-notes",
        "product-documents",
    }

    settings = _get(loaded_config, "syncsage") if isinstance(loaded_config, Mapping) else loaded_config
    state_value = str(_get(settings, "state_path"))
    vault_value = str(_get(settings, "vault_path"))
    assert str(state_path) in state_value
    assert str(vault_path) in vault_value


def test_config_validation_reports_missing_source_path(tmp_path: Path, config_path: Path) -> None:
    """Invalid configured paths should be rejected or reported clearly."""

    bad_config = tmp_path / "missing-source.yaml"
    bad_config.write_text(
        config_path.read_text(encoding="utf-8").replace("/syncsage-repo", "/missing-repo"),
        encoding="utf-8",
    )

    config_module = import_any(("syncsage.config.loader", "syncsage.config"))
    loader = require_attr(config_module, ("load_config", "load_syncsage_config", "load", "from_yaml"), "config loader")
    try:
        loaded = loader(bad_config)
    except Exception as exc:  # noqa: BLE001 - acceptance test checks user-facing validation text.
        message = str(exc).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return

    validator = first_attr(config_module, ("validate_config", "validate", "validate_paths", "validate_source_paths")) or first_attr(
        loaded, ("validate", "validate_paths")
    )
    assert validator is not None, "Config loader must validate paths or expose an explicit validator."
    try:
        validation_result = validator(loaded, require_exists=True)
    except TypeError:
        try:
            validation_result = validator(loaded)
        except TypeError:
            validation_result = validator()
    except Exception as exc:  # noqa: BLE001 - acceptance test checks user-facing validation text.
        message = str(exc).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return

    if validation_result:
        message = str(validation_result).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return
    raise AssertionError("Config validation accepted a source path that does not exist.")


def test_cli_validate_accepts_rendered_config(config_path: Path) -> None:
    """The user-facing `syncsage validate` command should accept the example config."""

    result = call_cli(["validate", str(config_path)])
    assert result.returncode == 0, result.stderr or result.stdout
    assert "error" not in result.stderr.lower()


def test_graph_section_of_the_yaml_is_actually_applied() -> None:
    """`SyncSageConfig.model_validate` must respect a `graph:` YAML section.

    Regression test: `graph=build(GraphSettings, data.get("graph"))` was
    missing from `model_validate`'s constructor call, so ANY `graph:`
    section in a config file — `concept_min_documents`,
    `wasm_cross_source_resolution` — was silently discarded in favor of
    `GraphSettings()` defaults, with no error and no test catching it.
    Found live: `docker-compose`-deployed config showed
    `wasm_cross_source_resolution: false` in the resolved `/config`
    response despite the mounted YAML setting it `true`.
    """
    from syncsage.config.schema import SyncSageConfig

    config = SyncSageConfig.model_validate(
        {
            "syncsage": {"name": "graph-section-regression"},
            "graph": {
                "concept_min_documents": 5,
                "wasm_cross_source_resolution": True,
            },
        }
    )
    assert config.graph.concept_min_documents == 5
    assert config.graph.wasm_cross_source_resolution is True
