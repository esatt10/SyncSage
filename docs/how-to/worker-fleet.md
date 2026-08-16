# Running a worker fleet

Indexing is CPU-bound Python. One container can only parse as fast as its own
cores, and while it does, it is holding the GIL against the request path. A
**preparation worker** is a second process — usually a second container — that
does the parsing and chunking and nothing else.

This page is about running several of them safely. If you only want one
container to go faster, [speed up indexing](indexing-performance.md) is the
shorter answer.

## What a worker is, and is not

A worker receives a file's bytes and a whitelisted parse config, and returns
chunks. That is the whole contract, and everything else follows from it:

* it **never writes** SQLite, the graph, manifests or vectors — the coordinator
  commits, in discovery order, so stable IDs and graph bytes stay deterministic;
* it **never receives connector credentials**. Only parsing inputs cross the
  boundary, so a compromised worker cannot reach your Notion or Drive;
* it holds no state between requests, so workers are interchangeable and
  disposable.

That last property is what makes the durability below possible: a request that
fails can simply be sent somewhere else.

## Start a fleet

Each worker is an ordinary pheasant container with the worker role turned on
and the shared token in its environment:

```yaml
# worker container
sync:
  concurrency:
    remote_worker_enabled: true
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
```

```bash
# HTTP workers are the API server
pheasant serve

# gRPC workers are their own process
pheasant worker --transport grpc --port 8766
```

The coordinator names them:

```yaml
sync:
  concurrency:
    file_executor: remote
    remote_worker_urls:
      - http://worker-a:8765
      - http://worker-b:8765
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
    remote_worker_batch_size: 8
    worker_transport: http    # or grpc
```

!!! warning "`/internal/indexing/prepare*` must not face the public internet"

    It is bearer-authenticated and refuses to run at all unless
    `remote_worker_enabled` is set, but it exists to accept work from your own
    coordinator. Keep it on an internal network or behind an ingress that
    does not route to it.

## What happens when a worker dies

Nothing you have to configure, which is the point. Dispatch is durable by
default:

| Failure | What happens |
|---|---|
| One worker refuses connections | Three attempts, then its circuit opens for 30 s and the rest of the sync goes elsewhere |
| A worker is slow | The request carries the caller's remaining budget; the worker declines work whose caller has already given up |
| A worker returns 429 or 503 | Retried with full jitter, honouring `Retry-After` |
| A rolling deploy replaces every pod | Endpoints fail over; if none answer, the coordinator prepares locally |
| **Every worker is down** | The sync **still completes**, with an identical index — only slower |

That last row is the design rule: remote preparation is a throughput
optimization, so it can never fail a sync. A test asserts the artifacts are
byte-identical between a healthy fleet and a completely dead one.

A retried request carries the same content-addressed idempotency key, so a
worker that already did the work answers from cache rather than parsing twice.

### Watching it

```promql
pheasant_worker_up{endpoint="http://worker-a:8765"}
```

`0` means that endpoint's breaker is open. Sync throughput
(`pheasant_index_files_per_second`) is the number that tells you whether it
mattered — see [monitor indexing](monitor-indexing.md).

## HTTP or gRPC

Start with HTTP. It needs no extra, and every durability property above is
identical on both transports because they are implemented once, above the
transport.

Choose gRPC (`pip install 'pheasant[grpc]'`, `worker_transport: grpc`) when:

* **your corpus is large and your network is not free.** JSON base64s file
  content, a flat 33% inflation on the only large field; gRPC sends it raw.
* **you have slow outliers.** `PrepareBatch` streams, so results come back as
  they are computed and one refused file does not fail its whole batch — over
  HTTP a batch answers with one body, so a single unacceptable file sends the
  rest back for local preparation.

The `.proto` ships in the package
(`pheasant/sync/proto/pheasant_worker.proto`) and is compiled at import time,
so it is a real contract you can implement a worker against in any language.

## Batch size is a memory knob

`remote_worker_batch_size` amortizes per-request overhead, but every task in a
batch holds its file's bytes in memory on **both** sides. With the default
25 MB file limit, a batch of 8 is a 200 MB worst case per in-flight batch, and
there are `max_parallel_files` of those. Raise it for a corpus of small files;
leave it alone if your sources contain large documents.

## Queue the backlog

Separately from workers: with several sources, the list of what is left to
index lives in the coordinator's memory. A process killed nine sources into ten
has lost the tenth.

```yaml
sync:
  queue:
    enabled: true
    backend: local      # this knowledge base's own database — no broker
```

Now the backlog is rows. A restart resumes it, `pheasant_index_queue_depth` is
a number other processes can read, and a source that keeps failing is
dead-lettered after `max_attempts` instead of consuming the fleet forever.

```bash
pheasant queue status         # backlog, in-flight, and why anything died
pheasant queue drain          # index everything currently queued
pheasant queue requeue-dead   # replay dead letters once the cause is fixed
```

Redelivery is safe: indexing is idempotent by content hash and stable ID, so a
task delivered twice re-indexes to identical state.

Use `backend: nats` (`pip install 'pheasant[queue]'`) when indexers on
different machines must share one backlog and you would rather not point them
all at one database:

```yaml
sync:
  queue:
    enabled: true
    backend: nats
    nats_servers: ["nats://nats:4222"]
```

It buys fan-out, not correctness the local queue lacks — and it is one more
thing to run.

## Related

- [Speed up indexing](indexing-performance.md) — worker counts and executors.
- [Monitor indexing](monitor-indexing.md) — throughput, ETA, stall detection.
- [Capacity planning](capacity-planning.md) — when one region should become several.
