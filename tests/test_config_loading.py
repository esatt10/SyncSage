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


def test_config_loads_example_yaml_with_expected_sources(
    loaded_config: object, state_path: Path
) -> None:
    """The rendered example config should load and preserve configured paths/sources."""

    assert _source_names(loaded_config) == {
        "pheasant-repo",
        "architecture-notes",
        "product-documents",
    }

    settings = (
        _get(loaded_config, "pheasant") if isinstance(loaded_config, Mapping) else loaded_config
    )
    state_value = str(_get(settings, "state_path"))
    assert str(state_path) in state_value


def test_config_validation_reports_missing_source_path(tmp_path: Path, config_path: Path) -> None:
    """Invalid configured paths should be rejected or reported clearly."""

    bad_config = tmp_path / "missing-source.yaml"
    bad_config.write_text(
        config_path.read_text(encoding="utf-8").replace("/pheasant-repo", "/missing-repo"),
        encoding="utf-8",
    )

    config_module = import_any(("pheasant.config.loader", "pheasant.config"))
    loader = require_attr(
        config_module, ("load_config", "load_pheasant_config", "load", "from_yaml"), "config loader"
    )
    try:
        loaded = loader(bad_config)
    except Exception as exc:  # noqa: BLE001 - acceptance test checks user-facing validation text.
        message = str(exc).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return

    validator = first_attr(
        config_module, ("validate_config", "validate", "validate_paths", "validate_source_paths")
    ) or first_attr(loaded, ("validate", "validate_paths"))
    assert validator is not None, (
        "Config loader must validate paths or expose an explicit validator."
    )
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
    """The user-facing `pheasant validate` command should accept the example config."""

    result = call_cli(["validate", str(config_path)])
    assert result.returncode == 0, result.stderr or result.stdout
    assert "error" not in result.stderr.lower()


def test_graph_section_of_the_yaml_is_actually_applied() -> None:
    """`PheasantConfig.model_validate` must respect a `graph:` YAML section.

    Regression test: `graph=build(GraphSettings, data.get("graph"))` was
    missing from `model_validate`'s constructor call, so ANY `graph:`
    section in a config file — `concept_min_documents`,
    `wasm_cross_source_resolution` — was silently discarded in favor of
    `GraphSettings()` defaults, with no error and no test catching it.
    Found live: `docker-compose`-deployed config showed
    `wasm_cross_source_resolution: false` in the resolved `/config`
    response despite the mounted YAML setting it `true`.
    """
    from pheasant.config.schema import PheasantConfig

    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "graph-section-regression"},
            "graph": {
                "concept_min_documents": 5,
                "wasm_cross_source_resolution": True,
            },
        }
    )
    assert config.graph.concept_min_documents == 5
    assert config.graph.wasm_cross_source_resolution is True


def test_example_config_has_no_duplicate_top_level_keys() -> None:
    """`pheasant.example.yaml` must not define the same top-level key twice.

    Regression test for a real defect in the shipped reference config: it had
    **two** `sync:` blocks. YAML keeps the last occurrence, so the entire
    documented `sync.limits` guardrail block — the one whose own comments
    describe stopping "I accidentally indexed my home directory" — was
    silently discarded, with no error from any parser and no test catching it.
    A duplicate key here is always a bug: it means a section the file appears
    to document has no effect.
    """
    import re

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    keys = re.findall(r"^([a-z_]+):", example.read_text(encoding="utf-8"), flags=re.M)
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"duplicate top-level keys in pheasant.example.yaml: {duplicates}"


def test_example_config_still_declares_the_sync_guardrails() -> None:
    """The guardrails must survive as *loaded* config, not just as text."""
    import yaml

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    limits = (data.get("sync") or {}).get("limits") or {}
    assert limits.get("max_files")
    assert limits.get("max_file_size_mb")
    assert limits.get("follow_symlinks") is False


def test_example_config_declares_the_document_extractor() -> None:
    import yaml

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    extractor = (data.get("ingestion") or {}).get("extractor") or {}
    assert extractor.get("provider") == "auto"
    assert extractor.get("html_text") is False


def test_yaml_shim_strips_trailing_comments_like_pyyaml() -> None:
    """The dependency-light YAML shim must drop trailing `# ...` comments.

    Regression test: it stripped whole-line comments but not trailing ones, so
    every annotated value in `pheasant.example.yaml` parsed with the comment
    glued on — `max_files: 50000  # ...` became a *string*, and
    `follow_symlinks: false  # ...` became a non-empty (therefore **truthy**)
    string, silently inverting a safety default wherever this shim stands in
    for PyYAML.
    """
    import yaml

    parsed = yaml.safe_load(
        "limits:\n"
        "  max_files: 50000        # matching files\n"
        "  follow_symlinks: false  # links escape or loop\n"
        "  label: red#1\n"
        '  quoted: "has # inside"\n'
    )
    limits = parsed["limits"]
    assert limits["max_files"] == 50000
    assert limits["follow_symlinks"] is False
    assert limits["label"] == "red#1"  # '#' without leading space is literal
    assert limits["quoted"] == "has # inside"  # '#' inside quotes is literal


def test_yaml_shim_keeps_urls_in_lists_as_strings_like_pyyaml() -> None:
    """A colon only separates a YAML key when followed by space or EOL.

    Regression test: the shim split list items on *any* colon, so
    `- http://host:8765` parsed as `{"http": "//host:8765"}` — silently
    turning every list-of-URL config into a list of dicts wherever this shim
    stands in for PyYAML. `server.api.cors_origins` is the one that caught it
    (a dict there breaks CORS *and* the MCP transport guard derived from it),
    but the same shape appears anywhere a URL is listed.
    """
    import yaml

    parsed = yaml.safe_load(
        "server:\n"
        "  api:\n"
        "    cors_origins:\n"
        "      - http://localhost:8765\n"
        "      - https://pheasant.internal:443\n"
        "sources:\n"
        "  - name: repo\n"
        "    type: git_repository\n"
    )
    assert parsed["server"]["api"]["cors_origins"] == [
        "http://localhost:8765",
        "https://pheasant.internal:443",
    ]
    # A genuine mapping item (colon *then space*) still parses as a mapping.
    assert parsed["sources"] == [{"name": "repo", "type": "git_repository"}]


def test_config_with_removed_obsidian_settings_still_loads(tmp_path: Path) -> None:
    """A pre-removal config must keep loading, not hard-fail.

    The Obsidian projection went away, but the YAML files describing it are
    user data sitting on real disks. `model_validate` drops unknown keys, so
    this would pass silently even without the shim — what the shim adds, and
    what this pins, is that the removal is *reported* rather than ignored.
    """

    from pheasant.config.loader import (
        REMOVED_SETTINGS,
        load_config,
        load_layered_config,
        warn_on_removed_settings,
    )

    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(
        "pheasant:\n"
        "  name: legacy-kb\n"
        f"  state_path: {tmp_path / 'state'}\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  exports_path: {tmp_path / 'exports'}\n"
        f"  workspace_root: {tmp_path}\n"
        "obsidian:\n"
        "  enabled: true\n"
        "  template_profile: engineering\n"
        "sources: []\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.pheasant.name == "legacy-kb"
    assert not hasattr(config, "obsidian")
    assert not hasattr(config.pheasant, "vault_path")

    # Both loaders report both removed keys, and every key carries guidance.
    raw = {"pheasant": {"vault_path": "/vault"}, "obsidian": {"enabled": True}}
    assert sorted(warn_on_removed_settings(raw)) == ["obsidian", "pheasant.vault_path"]
    assert all(REMOVED_SETTINGS[key] for key in REMOVED_SETTINGS)

    layered = load_layered_config(config_path, profile="dev", overrides={})
    assert layered.pheasant.name == "legacy-kb"


def test_removed_setting_warning_is_logged_once_per_process(tmp_path: Path, caplog: object) -> None:
    """Config is re-read constantly; a notice that repeats gets filtered out."""

    import logging

    from pheasant.config import loader as loader_module

    loader_module._WARNED.discard("obsidian")
    raw = {"obsidian": {"enabled": True}}
    with caplog.at_level(logging.WARNING, logger="pheasant.config.loader"):  # type: ignore[attr-defined]
        loader_module.warn_on_removed_settings(raw)
        loader_module.warn_on_removed_settings(raw)

    obsidian_warnings = [r for r in caplog.records if "obsidian" in r.getMessage()]  # type: ignore[attr-defined]
    assert len(obsidian_warnings) == 1
    assert "graph workspace" in obsidian_warnings[0].getMessage()
