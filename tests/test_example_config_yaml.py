"""Guard: the shipped example config must be valid under *real* PyYAML.

The container smoke test bakes ``pheasant.example.yaml`` into the image and
parses it with the installed PyYAML (a hard dependency, ``PyYAML>=6.0``). The
rest of the test suite, however, resolves the repo-root ``yaml.py`` *shim* — a
deliberately lenient, dependency-light YAML subset — so a strict-YAML error in
the example config (e.g. a misindented key) sails past ``pytest`` and only
fails later in the slow Docker smoke job.

This module closes that gap by parsing the shipped config in a **subprocess
run from outside the repo**, where ``import yaml`` resolves to the genuine
site-packages PyYAML (exactly as it does in the container) rather than the
repo-root shim. A malformed example config now fails in seconds on every PR.
When real PyYAML is unavailable (the dependency-light env the shim exists for),
the test skips rather than giving a false pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO / "pheasant.example.yaml"

# Parse a YAML file with whatever ``yaml`` the interpreter imports, printing a
# stable marker we can distinguish from a genuine parse error.
_PARSE_SNIPPET = (
    "import sys\n"
    "try:\n"
    "    import yaml\n"
    "except ModuleNotFoundError:\n"
    "    print('NO_PYYAML'); sys.exit(3)\n"
    "if getattr(yaml, 'scanner', None) is None:\n"  # the shim has no scanner
    "    print('NO_PYYAML'); sys.exit(3)\n"
    "with open(sys.argv[1], encoding='utf-8') as fh:\n"
    "    yaml.safe_load(fh)\n"
    "print('PARSE_OK')\n"
)


def _parse_with_real_pyyaml(yaml_path: Path) -> subprocess.CompletedProcess:
    """Run the parse snippet in a subprocess whose cwd is *not* the repo.

    Scrubs the repo root from ``PYTHONPATH`` too, so the repo-root ``yaml.py``
    shim can never shadow the installed PyYAML.
    """
    env = dict(os.environ)
    if env.get("PYTHONPATH"):
        parts = [p for p in env["PYTHONPATH"].split(os.pathsep) if p and Path(p).resolve() != REPO]
        env["PYTHONPATH"] = os.pathsep.join(parts)
    return subprocess.run(
        [sys.executable, "-c", _PARSE_SNIPPET, str(yaml_path)],
        cwd=str(REPO.parent),  # outside the repo → no shim on sys.path
        env=env,
        capture_output=True,
        text=True,
    )


def test_example_config_parses_under_real_pyyaml() -> None:
    result = _parse_with_real_pyyaml(EXAMPLE_CONFIG)
    if result.returncode == 3 or "NO_PYYAML" in result.stdout:
        pytest.skip("real PyYAML not installed (dependency-light shim env)")
    assert result.returncode == 0, (
        "pheasant.example.yaml failed to parse under real PyYAML "
        "(this is exactly what the container smoke test does):\n" + result.stderr
    )
    assert "PARSE_OK" in result.stdout


def test_real_pyyaml_guard_would_catch_the_regression(tmp_path: Path) -> None:
    """Sanity: the real parser rejects the exact mistake the smoke test hit.

    A misindented key under a scalar sibling (the ``keep_event_days`` bug) must
    make the subprocess exit non-zero — proving this guard actually bites, not
    just that the current file happens to be valid.
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text("storage:\n  max_state_size_gb: 10\n    keep_event_days: 30\n")
    result = _parse_with_real_pyyaml(bad)
    if result.returncode == 3 or "NO_PYYAML" in result.stdout:
        pytest.skip("real PyYAML not installed (dependency-light shim env)")
    assert result.returncode != 0 and "PARSE_OK" not in result.stdout, (
        "the real-PyYAML guard did not reject a misindented mapping; "
        "the regression check is not actually biting"
    )


# ---------------------------------------------------------------------------
# Every config pheasant *writes* must be readable by the dependency-light YAML
# shim, not just by PyYAML.
#
# Found in a live run, not by this suite: `yaml` in the test environment
# resolves to PyYAML, so a round-trip test that just calls `yaml.safe_load`
# proves nothing about the shim. These load it by path.
# ---------------------------------------------------------------------------


def _shim():
    """Import the repo-root `yaml.py` shim explicitly, whatever `yaml` resolves to."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "yaml.py"
    spec = importlib.util.spec_from_file_location("pheasant_yaml_shim", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pheasant_up_writes_a_config_the_shim_can_read(tmp_path: Path) -> None:
    """Regression: PyYAML writes block sequences at the *parent's* indent, the
    shim requires +2, and every config `pheasant up` ever wrote blew up with
    `AttributeError: 'dict' object has no attribute 'append'`."""
    from pheasant.cli import main

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n\nProse.\n", encoding="utf-8")
    config_path = tmp_path / "pheasant.yaml"
    assert main(["up", str(corpus), "-c", str(config_path), "--no-serve"]) == 0

    data = _shim().safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["sources"][0]["name"] == "corpus"
    assert isinstance(data["security"]["allow_workspace_roots"], list)


def test_pheasant_setup_writes_a_config_the_shim_can_read(tmp_path: Path) -> None:
    from pheasant.cli import main

    config_path = tmp_path / "pheasant.yaml"
    assert main(["setup", "--accept-defaults", "-o", str(config_path)]) == 0

    data = _shim().safe_load(config_path.read_text(encoding="utf-8"))
    # A nested list inside a nested mapping — the exact shape that broke.
    assert data["assistant"]["retrieval"]["retrieval_modes"] == ["text", "vector"]


def test_a_list_item_containing_a_colon_survives_the_shim() -> None:
    """Unquoted, the shim splits `- http://localhost:5173` into a mapping —
    silently wrong data on the setting that decides which browser origins may
    drive an unauthenticated API."""
    from pheasant.config.loader import dump_config_yaml

    payload = {"server": {"api": {"cors_origins": ["http://localhost:5173"]}}}
    parsed = _shim().safe_load(dump_config_yaml(payload))
    assert parsed["server"]["api"]["cors_origins"] == ["http://localhost:5173"]


def test_the_dumper_output_is_also_valid_for_pyyaml() -> None:
    """Fixing the shim must not break the reader almost everyone actually uses."""
    import yaml as real_yaml

    from pheasant.config.loader import dump_config_yaml

    payload = {"a": {"b": ["x:y", "z"], "c": 1}, "d": True}
    assert real_yaml.safe_load(dump_config_yaml(payload)) == payload
