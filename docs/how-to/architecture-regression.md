# Run the architecture regression benchmark

The CI architecture gate exercises realistic repository shapes without an LLM,
embedding provider, API key, or metered network call during indexing. Its only
network phase is the initial public Git checkout.

## What it tests

The manifest in `benchmarks/repository-architecture-budget.json` uses sparse,
shallow checkouts of:

- Apache Spark (`python/pyspark`);
- MLflow (`mlflow`);
- Visual Studio Code (`src/vs`);
- LangGraph (`libs/langgraph`); and
- Deep Agents (`libs/deepagents`).

Two hundred eligible, natively supported text files are selected per repository
by a SHA-256 ordering of relative paths. The selection is deterministic for a
given upstream commit, capped at 512 KiB per file, and applies the same
secret/noise excludes as indexing. The report records each commit and the
selected-path digest.

The manifest pins the exact upstream commits used by the accepted baseline.
Update a pin deliberately, run the benchmark, and review both correctness and
timing before committing it. This prevents an unrelated upstream merge from
appearing as a Pheasant regression on a later pull request.

Each run checks:

1. a full index of all five source scopes;
2. an unchanged incremental pass that indexes nothing and makes no new
   embedding calls;
3. vector, graph, and hybrid retrieval for every source; and
4. full/incremental wall time, full throughput, search p95, and peak RSS
   against committed guardrails.

The embedder is the deterministic 64-dimension `stub`, the vector store is
local NumPy, the assistant is disabled with provider `none`, and watchers and
schedulers are disabled. Blank provider environment variables in CI add a
second guard against accidentally turning the test into a paid online job.

## Run it locally

From the repository root:

```bash
python scripts/checkout_repository_corpus.py \
  --manifest benchmarks/repository-architecture-budget.json \
  --output .ci-corpus --jobs 3

python -m pheasant.sync.repository_benchmark \
  --manifest benchmarks/repository-architecture-budget.json \
  --repositories-root .ci-corpus \
  --output architecture-benchmark.json
```

The command exits non-zero if a correctness invariant or performance budget
fails. Inspect `checks`, per-source `full` and `incremental` rows, search runs,
and `correctness` in the report before changing a budget.

## Changing a budget

Do not lower the test corpus or loosen a limit only to make a pull request
green. First compare the uploaded JSON report with a successful run and decide
whether the change is real work, shared-runner noise, or an intentional
architecture trade. A budget change should carry that evidence in the commit
or pull request.

The generous absolute ceilings catch large regressions and hangs; the
incremental/full ratio catches new work that scales with corpus size. Provider
latency and quota are deliberately outside this gate and belong in a separate
credentialed environment. The fleet's operational scaling defaults are also
outside this benchmark and remain unchanged.

## Initial local baseline

The 2026-08-27 Windows run used the five upstream commits recorded in its JSON
report. LangGraph and Deep Agents contained fewer than 200 eligible files in
their sparse scopes, producing 925 files total.

| Measurement | Result |
|---|---:|
| Full index | 43.39 s |
| Full throughput | 21.32 files/s |
| Unchanged incremental | 4.02 s |
| Incremental/full ratio | 0.093 |
| Retrieval median | 82 ms |
| Retrieval p95 | 106 ms |
| Incremental embedding calls | 0 |
| Correctness/performance checks | all passed |

Shared GitHub runners will not reproduce workstation timings exactly. The
committed limits are intentionally wider than this baseline, while the JSON
artifact preserves the per-commit evidence needed to tighten them after
several Linux CI observations.
