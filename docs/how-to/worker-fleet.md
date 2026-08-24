# Running a worker fleet

## Split the process first

Before any of the below matters, decide what each process *is*. One pheasant
process serves search, serves the UI, watches directories, runs the scheduler
and indexes — right for one container, wrong for several, because three
replicas of it all watch the same directories and all try to index the same
source.

```bash
pheasant serve                    # all: today's behavior, the default
pheasant serve --role api         # serve; publish index work to the queue
pheasant serve --role indexer     # watch, schedule, drain the queue
pheasant worker --transport grpc  # preparation only
```

`api` replicas scale with request traffic and never index; one `indexer` per
shard does the indexing; `worker` pods do the parsing. The hand-off between
api and indexer is the [queue](#queue-the-backlog), which is why `--role api`
refuses to start without it — a sync request that is accepted and then goes
nowhere is worse than one that is refused.

`GET /health` and `GET /ready` report the role, so you can tell pods apart
from a probe response. Full table in
[configuration](../configuration.md#process-roles-serverrole).


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

## Running the whole fleet

The pieces above assemble into three tiers. Both runtimes ship a working
version, and both are the *other* trade from the single container — take them
only past the point where one container stops being enough
([capacity planning](capacity-planning.md)).

### Compose

```bash
export PHEASANT_INDEX_WORKER_TOKEN=$(openssl rand -hex 32)
export OPENAI_API_KEY=...
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up -d \
  --scale indexer=4 --scale worker=8
```

Postgres, NATS JetStream, one API, four indexers and eight gRPC workers. The
durable consumer distributes source tasks and PostgreSQL leases writes per
source, so indexers can work on different sources at the same time. A single
source still has one write lease: scaling indexers helps a multi-source corpus,
while gRPC workers fan out parsing and chunking within the source being
processed. The gRPC coordinator keeps one multiplexed channel to Docker's
scaled service name and selects `round_robin`, so concurrent preparation
batches reach distinct worker replicas instead of gRPC's `pick_first` default
pinning the source to one container. Scale API replicas only after putting a
load balancer in front of them; Compose deliberately publishes one API endpoint.

### Kubernetes

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl -n pheasant create secret generic pheasant-secrets \
  --from-literal=PHEASANT_DATABASE_URL='postgresql://…' \
  --from-literal=PHEASANT_INDEX_WORKER_TOKEN="$(openssl rand -hex 32)"
kubectl apply -f deploy/kubernetes/scaled/
```

| Workload | Kind | Scales on |
|---|---|---|
| `pheasant-api` | Deployment | CPU (HPA) |
| `pheasant-indexer` | StatefulSet, 1 replica | not autoscaled |
| `pheasant-worker` | Deployment | `pheasant_index_queue_depth` (KEDA or HPA) |

`deploy/kubernetes/scaled/README.md` lists what you must provide first. Two
requirements are easy to miss and neither is optional:

* **A ReadWriteMany volume for `/state`.** The knowledge graph is a file the
  indexer writes and every api replica reads. With RWO the volume attaches to
  one node, so api replicas scheduled elsewhere cannot read it at all. Most
  default StorageClasses are RWO.
* **Postgres.** SQLite permits one writer per file, and it is not one file
  across pods.

Api replicas poll the graph file every `server.api.graph_refresh_seconds`
(30 s) and reload when it changes. Without that they would answer graph
queries from whatever the graph was when the pod started — while text and
vector search stayed current from the shared database, which is exactly what
makes the staleness easy to miss.

### Scale on the backlog, not on CPU

CPU is a lagging signal for the worker tier: workers only get busy once the
indexer is already sending them work, so a CPU-driven autoscaler adds capacity
after the queue has built up. `pheasant_index_queue_depth` rises the moment
sources are enqueued.

```promql
sum(pheasant_index_queue_depth) or vector(0)
```

`deploy/kubernetes/scaled/worker-hpa.yaml` ships both a KEDA `ScaledObject`
(preferred — reads Prometheus directly and can scale to **zero**, which
matters because idle workers are pure cost) and a plain HPA for clusters
without it.

The api tier scales on CPU with a five-minute scale-down window, because a new
replica loads the whole graph into memory at startup: shedding a replica
eagerly and adding it back pays that twice.

## Related

- [Speed up indexing](indexing-performance.md) — worker counts and executors.
- [Monitor indexing](monitor-indexing.md) — throughput, ETA, stall detection.
- [Capacity planning](capacity-planning.md) — when one region should become several.
