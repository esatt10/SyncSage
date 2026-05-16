from __future__ import annotations

from pathlib import Path

_src_pkg = Path(__file__).resolve().parents[1] / "src" / "syncsage"
if _src_pkg.exists():
    __path__.append(str(_src_pkg))  # type: ignore[name-defined]

__version__ = "0.1.0"
