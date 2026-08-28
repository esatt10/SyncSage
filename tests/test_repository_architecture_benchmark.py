from __future__ import annotations

import json
from pathlib import Path

from pheasant.ingestion.content_types import TEXT_EXTENSIONS
from pheasant.sync.repository_benchmark import (
    REQUIRED_REPOSITORIES,
    RETRIEVAL_MODES,
    SOURCE_EXTENSIONS,
    load_manifest,
    run_repository_benchmark,
)

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "benchmarks" / "repository-architecture-budget.json"


def test_ci_manifest_uses_the_five_public_repository_regression_corpora() -> None:
    manifest = load_manifest(MANIFEST)
    repositories = {item["name"]: item for item in manifest["repositories"]}

    assert set(repositories) == REQUIRED_REPOSITORIES
    assert repositories["spark"]["url"] == "https://github.com/apache/spark.git"
    assert repositories["mlflow"]["url"] == "https://github.com/mlflow/mlflow.git"
    assert repositories["vscode"]["url"] == "https://github.com/microsoft/vscode.git"
    assert repositories["langgraph"]["url"] == "https://github.com/langchain-ai/langgraph.git"
    assert repositories["deepagents"]["url"] == "https://github.com/langchain-ai/deepagents.git"
    assert all(
        len(str(repository["ref"])) == 40
        and all(character in "0123456789abcdef" for character in repository["ref"])
        for repository in repositories.values()
    )
    assert RETRIEVAL_MODES == ("vector", "graph", "hybrid")
    assert SOURCE_EXTENSIONS == TEXT_EXTENSIONS


def test_repository_architecture_benchmark_is_offline_idempotent_and_searchable(
    tmp_path: Path,
) -> None:
    repositories = []
    corpus_root = tmp_path / "corpus"
    for name in sorted(REQUIRED_REPOSITORIES):
        source = corpus_root / name
        source.mkdir(parents=True)
        (source / "architecture.py").write_text(
            f"class {name.title().replace('agents', 'Agents')}Worker:\n"
            "    def index_graph(self):\n"
            "        return 'checkpoint vector graph search'\n",
            encoding="utf-8",
        )
        (source / "README.md").write_text(
            f"# {name}\n\nGraph checkpoint vector worker architecture.\n",
            encoding="utf-8",
        )
        repositories.append(
            {
                "name": name,
                "url": f"https://github.com/example/{name}.git",
                "ref": "0" * 40,
                "sparse_paths": ["."],
                "query": "graph checkpoint vector worker architecture",
            }
        )

    manifest = {
        "version": 1,
        "sample_files_per_repository": 2,
        "max_file_bytes": 10000,
        "repositories": repositories,
        "budgets": {
            "max_full_seconds": 120.0,
            "min_full_files_per_second": 0.01,
            "max_incremental_seconds": 120.0,
            "max_incremental_to_full_ratio": 10.0,
            "max_search_p95_seconds": 30.0,
            "max_peak_rss_mb": 4096.0,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "report.json"

    report = run_repository_benchmark(manifest_path, corpus_root, output)

    assert report["passed"] is True
    assert report["offline"] == {"llm": "disabled", "embeddings": "stub", "network_calls": 0}
    assert report["retrieval_modes"] == ["vector", "graph", "hybrid"]
    assert report["totals"]["files"] == 10
    assert all(report["correctness"].values())
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
