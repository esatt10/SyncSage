"""Shallow, sparse checkout of the public repositories used by CI.

This is the benchmark's only network step. Indexing and retrieval use the
deterministic stub embedder and never call an LLM or embedding service.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def _checkout(repository: dict[str, Any], root: Path) -> dict[str, str]:
    name = str(repository["name"])
    url = str(repository["url"])
    ref = str(repository.get("ref") or "").strip()
    target = root / name
    if target.exists():
        raise FileExistsError(f"checkout target already exists: {target}")
    subprocess.run(
        [
            "git",
            "-c",
            "core.longpaths=true",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--sparse",
            "--no-tags",
            url,
            str(target),
        ],
        check=True,
    )
    sparse_paths = [str(item) for item in repository.get("sparse_paths") or []]
    if sparse_paths:
        subprocess.run(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "-C",
                str(target),
                "sparse-checkout",
                "set",
                *sparse_paths,
            ],
            check=True,
        )
    if ref:
        subprocess.run(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "-C",
                str(target),
                "fetch",
                "--depth",
                "1",
                "--filter=blob:none",
                "--no-tags",
                "origin",
                ref,
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "-C",
                str(target),
                "checkout",
                "--detach",
                "FETCH_HEAD",
            ],
            check=True,
        )
    commit = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if ref and commit != ref:
        raise RuntimeError(f"repository {name!r} resolved {commit}, expected pinned ref {ref}")
    return {"name": name, "url": url, "commit": commit}


def checkout_manifest(manifest_path: Path, output: Path, jobs: int = 3) -> list[dict[str, str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repositories = list(manifest.get("repositories") or [])
    if not repositories:
        raise ValueError("benchmark manifest contains no repositories")
    output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, min(int(jobs), len(repositories)))) as pool:
        checkouts = list(pool.map(lambda repo: _checkout(repo, output), repositories))
    return checkouts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    checkouts = checkout_manifest(args.manifest, args.output, args.jobs)
    print(json.dumps({"checkouts": checkouts}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CI
    raise SystemExit(main())
