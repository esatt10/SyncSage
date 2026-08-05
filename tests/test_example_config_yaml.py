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
        parts = [
            p
            for p in env["PYTHONPATH"].split(os.pathsep)
            if p and Path(p).resolve() != REPO
        ]
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
