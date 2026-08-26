from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_graph_workspace_defaults_to_concentric_layout() -> None:
    source = (REPO_ROOT / "ui" / "src" / "state" / "session.tsx").read_text(
        encoding="utf-8"
    )
    initial = re.search(r"const INITIAL: SessionState = \{(?P<body>.*?)\n\};", source, re.DOTALL)

    assert initial is not None
    assert re.search(r'^\s*layout: "concentric",$', initial.group("body"), re.MULTILINE)
