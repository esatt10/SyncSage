from __future__ import annotations

from pathlib import Path


class PathPolicyError(ValueError):
    pass


def resolve_under(path: str | Path, allowed_roots: list[str | Path]) -> Path:
    candidate = Path(path).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not roots:
        return candidate
    for root in roots:
        if candidate == root or root in candidate.parents:
            return candidate
    raise PathPolicyError(f"Path {candidate} is outside allowed roots: {', '.join(map(str, roots))}")
