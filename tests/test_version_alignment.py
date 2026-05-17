from __future__ import annotations

import subprocess
import sys

import pytest
from scripts.sync_version import check_image_version_increment

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


def test_image_version_check_rejects_duplicate_tag() -> None:
    with pytest.raises(SystemExit, match="already exists"):
        check_image_version_increment("1.2.3", {"1.2.3", "latest", "sha-deadbeef"})


def test_image_version_check_rejects_lower_or_equal_version() -> None:
    with pytest.raises(SystemExit, match="must be greater"):
        check_image_version_increment("1.2.3", {"v1.2.4"})


def test_image_version_check_accepts_next_higher_version() -> None:
    check_image_version_increment("1.3.0", {"v1.2.4", "latest", "sha-deadbeef"})
