# Speed up indexing

pheasant separates work that can scale from state that must stay ordered:

1. discover and stat items in stable order;
2. prepare files concurrently (read, SHA-256, skip unchanged, parse and chunk);
3. embed changed chunks in bounded provider-sized batches;
4. commit SQLite, graph, manifests and vectors through one coordinator;
5. run global graph enrichment and save.

This preserves stable IDs, incremental skips and deterministic graph bytes at
every worker count.

In a fleet, scale the **preparation workers**, not the indexer. Each shard has
one elected indexer/commit authority; additional indexer replicas are hot
standbys. If commit/enrichment/graph-save time dominates after preparation is
fast, split sources into another shard instead of adding writers to the same
graph.

Keep the dispatch window bounded. A remote batch holds every file's bytes on
the indexer and worker, so the practical in-flight payload is roughly
`max_parallel_files * remote_worker_batch_size`. The fleet profile uses 16 x
16 (256 files), four worker containers with two request threads each, and 8
embedding requests in flight. That leaves CPU and memory for Postgres, NATS,
the API and the graph owner on an 8-core development host.

## Choose a local executor

```yaml
sync:
  concurrency:
    max_parallel_sources: 2
    max_parallel_files: 8
    max_parallel_embeddings: 4
    file_executor: thread
    lock_timeout_seconds: 120
```

Use `thread` when reads, remote connectors or document handlers dominate. Use
`process` for CPU-heavy, ordinary text/code/Markdown corpora:

```yaml
sync:
  concurrency:
    max_parallel_files: 8
    file_executor: process
```

Process workers are capped by the CPU quota visible to the process. Sources
requiring local document/modal/taxonomy handler state, and repair passes that
inspect the live graph, fall back to thread workers safely.

Embedding concurrency is independent. Keep it within the provider's rate and
connection limits; retries already honor transient failures and `Retry-After`.

## Add remote worker nodes

Give every worker and coordinator the same secret environment variable:

```bash
export PHEASANT_INDEX_WORKER_TOKEN='replace-with-a-long-random-value'
```

On each worker:

```yaml
sync:
  concurrency:
    remote_worker_enabled: true
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
```

Run pheasant normally behind TLS or an authenticated private ingress. On the
coordinator:

```yaml
sync:
  concurrency:
    file_executor: remote
    max_parallel_files: 16
    remote_worker_urls:
      - https://index-worker-1.internal
      - https://index-worker-2.internal
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
    remote_worker_timeout_seconds: 120
```

The coordinator reads each connector payload and sends immutable text bytes plus
source/chunking metadata round-robin. Workers return deterministic parsed chunks
and never receive write access to `/state`. Binary documents, images/audio,
taxonomy-enabled sources and repair passes currently prepare locally because
they rely on local sidecars or handler/graph state.

Do not expose `/internal/indexing/prepare` to the public internet. It is disabled
by default and bearer-authenticated when enabled, but task payloads contain the
source text being indexed.

## Measure before tuning

```bash
python -m pheasant.sync.benchmark --workers 1,2,4,8
python -m pheasant.sync.benchmark --workers 1,2,4 --executor process
python -m pheasant.sync.benchmark --workers 1,2,4 --embeddings
```

The harness creates a deterministic temporary corpus, stays offline, warms
parser/filesystem caches, and prints median seconds, individual trials,
files/second and speedup for a clean full index and the immediately unchanged
incremental pass (`--repeats 3` by default). The incremental report includes
embedding calls and should always say zero. Also test truly cold storage on a
representative deployment; the harness deliberately warms caches so worker-count
comparisons are not biased by whichever run touched the fixture first.
