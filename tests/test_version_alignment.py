from __future__ import annotations

import subprocess
import sys

from tests.conftest import REPO_ROOT


def test_generated_version_references_match_pyproject() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/sync_version.py", "--check"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
