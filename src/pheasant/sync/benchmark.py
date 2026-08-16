"""Offline sync-throughput benchmark for local capacity planning.

Run with ``python -m pheasant.sync.benchmark``. The fixture is generated in a
temporary directory, uses deterministic text and optional stub embeddings, and
never calls a network service. Results are JSON so CI or a capacity worksheet
can compare worker counts without scraping prose.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.sync.engine import SyncEngine


def _fixture(root: Path, files: int, lines: int) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    body = "\n".join(
        f"Deterministic benchmark line {line}: parsing chunking graph embeddings."
        for line in range(lines)
    )
    for index in range(files):
        (workspace / f"document-{index:05d}.md").write_text(
            f"# Benchmark document {index}\n\n{body}\n",
            encoding="utf-8",
        )
    return workspace


def _config(
    root: Path,
    workspace: Path,
    workers: int,
    embeddings: bool,
    file_executor: str,
    run_id: str,
) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sync-benchmark",
                "state_path": str(root / f"state-{file_executor}-{workers}-{run_id}"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "storage": {"graph_snapshots": False, "graph_checkpoint_seconds": 0},
            "search": {
                "embeddings": {
                    "enabled": embeddings,
                    "provider": "stub",
                    "dimensions": 64,
                    "batch_size": 64,
                },
                "vector_store": {"provider": "numpy"},
            },
            "sync": {
                "concurrency": {
                    "max_parallel_sources": 1,
                    "max_parallel_files": workers,
                    "max_parallel_embeddings": min(workers, 4),
                    "file_executor": file_executor,
                }
            },
            "sources": [
                {
                    "name": "corpus",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


def run_benchmark(
    *,
    files: int = 200,
    lines: int = 80,
    workers: tuple[int, ...] = (1, 2, 4, 8),
    embeddings: bool = False,
    file_executor: str = "thread",
    repeats: int = 3,
) -> dict[str, Any]:
    workers = tuple(dict.fromkeys(max(1, int(count)) for count in workers))
    if not workers:
        raise ValueError("at least one worker count is required")
    root = Path(tempfile.mkdtemp(prefix="pheasant-sync-benchmark-"))
    try:
        workspace = _fixture(root, files, lines)
        repeats = max(1, int(repeats))

        def run_once(worker_count: int, run_id: str) -> tuple[float, float, Any, Any, int]:
            engine = SyncEngine(
                _config(
                    root,
                    workspace,
                    worker_count,
                    embeddings,
                    file_executor,
                    run_id,
                )
            )
            started = time.perf_counter()
            try:
                result = engine.sync_source("corpus", "full")
                full_elapsed = time.perf_counter() - started
                calls_before = engine.vectors.embedder.calls if engine.vectors is not None else 0
                incremental_started = time.perf_counter()
                incremental = engine.sync_source("corpus", "incremental")
                incremental_elapsed = time.perf_counter() - incremental_started
                calls_after = engine.vectors.embedder.calls if engine.vectors is not None else 0
            finally:
                engine.close()
            return (
                full_elapsed,
                incremental_elapsed,
                result,
                incremental,
                calls_after - calls_before,
            )

        # Warm imports, parser caches and the fixture's filesystem pages before
        # comparing capacities. Cold-storage latency is deployment-specific.
        run_once(workers[0], "warmup")
        full_samples: dict[int, list[float]] = {count: [] for count in workers}
        incremental_samples: dict[int, list[float]] = {count: [] for count in workers}
        results: dict[int, Any] = {}
        incremental_results: dict[int, Any] = {}
        incremental_embedding_calls: dict[int, int] = {}
        for repeat in range(repeats):
            offset = repeat % len(workers)
            cycle = workers[offset:] + workers[:offset]
            for worker_count in cycle:
                full_elapsed, incremental_elapsed, result, incremental, calls = run_once(
                    worker_count, f"trial-{repeat}"
                )
                full_samples[worker_count].append(full_elapsed)
                incremental_samples[worker_count].append(incremental_elapsed)
                results[worker_count] = result
                incremental_results[worker_count] = incremental
                incremental_embedding_calls[worker_count] = calls

        medians = {count: statistics.median(values) for count, values in full_samples.items()}
        incremental_medians = {
            count: statistics.median(values) for count, values in incremental_samples.items()
        }
        baseline = medians[workers[0]]
        runs = []
        for worker_count in workers:
            elapsed = medians[worker_count]
            incremental_elapsed = incremental_medians[worker_count]
            result = results[worker_count]
            incremental = incremental_results[worker_count]
            runs.append(
                {
                    "workers": worker_count,
                    "seconds": round(elapsed, 4),
                    "trial_seconds": [round(value, 4) for value in full_samples[worker_count]],
                    "files_per_second": round(files / elapsed, 2),
                    "speedup": round(baseline / elapsed, 3),
                    "indexed_artifacts": result.indexed_artifacts,
                    "graph_nodes": result.graph_nodes,
                    "graph_edges": result.graph_edges,
                    "unchanged_incremental_seconds": round(incremental_elapsed, 4),
                    "unchanged_incremental_trial_seconds": [
                        round(value, 4) for value in incremental_samples[worker_count]
                    ],
                    "unchanged_incremental_files_per_second": round(files / incremental_elapsed, 2),
                    "unchanged_incremental_indexed_artifacts": incremental.indexed_artifacts,
                    "unchanged_incremental_skipped_artifacts": incremental.skipped_artifacts,
                    "unchanged_incremental_embedding_calls": incremental_embedding_calls[
                        worker_count
                    ],
                }
            )
        return {
            "fixture": {"files": files, "lines_per_file": lines},
            "embeddings": "stub" if embeddings else "disabled",
            "file_executor": file_executor,
            "cache": "warm",
            "repeats": repeats,
            "runs": runs,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--embeddings", action="store_true")
    parser.add_argument("--executor", choices=("thread", "process"), default="thread")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    worker_counts = tuple(max(1, int(value)) for value in args.workers.split(",") if value.strip())
    print(
        json.dumps(
            run_benchmark(
                files=max(1, args.files),
                lines=max(1, args.lines),
                workers=worker_counts,
                embeddings=args.embeddings,
                file_executor=args.executor,
                repeats=max(1, args.repeats),
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
