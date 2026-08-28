# Architecture

pheasant is a local-first MCP context server with an admin API, source
registry, deterministic indexing pipeline, and text, vector, and graph search.
The single-container topology is the default. The role-split fleet is the
scale-out topology for a corpus or request load that has crossed the measured
single-process limits.

## Runtime flow

1. Load and validate `/config/pheasant.yaml`.
2. Register sources and repair missing search or graph state.
3. Discover files in a stable order and skip unchanged content by hash.
4. Prepare changed files concurrently: read, parse, extract, and chunk.
5. Batch embeddings, then commit relational rows, vectors, manifests, and
   graph mutations through one ordered writer.
6. Enrich and atomically publish the shard's graph snapshot.
7. Serve API, MCP, UI, and retrieval traffic from the published state.

No indexed repository code is executed. Stable IDs, content hashes, ordered
commits, and sorted graph serialization make retries and unchanged
incremental runs deterministic.

## Logical components

| Component | Responsibility |
|---|---|
| API/MCP/UI | Health, source control, sync submission, search, answering, and agent tools. |
| Source registry | Configured and runtime source metadata, lifecycle state, and audit history. |
| Indexer | Watch, schedule, drain durable work, coordinate preparation, and make the one authoritative commit per knowledge-base shard. |
| Preparation worker | Stateless read/parse/extract/chunk work over authenticated HTTP or gRPC. It has no database or connector credentials. |
| Graph-query service | Read-only graph search/traversal over an atomically refreshed snapshot. Fleet APIs use a bounded proxy and do not load the full graph. |
| PostgreSQL/SQLite | Artifacts, chunks, lexical ranking, manifests, leases, queue state, and metadata. |
| Vector store | Optional semantic vectors; NumPy and LanceDB are supported. |
| NATS JetStream | Durable source-task transport between API and indexer in the fleet. |

## Retrieval decision

The four request modes are different views over three physical retrieval
arms:

| Mode | What it contributes | Cost characteristic |
|---|---|---|
| `text` | Exact identifiers and lexical matches from database full-text ranking. | Can become the slowest arm for common terms in PostgreSQL. |
| `vector` | Semantic matches when wording differs. | Embedding plus vector scan. |
| `graph` | Symbols, entities, dependencies, and related nodes. | Graph-service query in the fleet. |
| `hybrid` | Runs text, vector, and graph concurrently and merges them with reciprocal-rank fusion. | Approximately the slowest healthy arm, not their sum. |

The scalable assistant fanout is `vector`, `graph`, and `hybrid`. A separate
`text` fanout was removed because `hybrid` already executes the same lexical
query, and stress testing measured high-frequency PostgreSQL text ranking as
the dominant search bottleneck. Paying for that query twice had weak recall
value. Standalone text search is not disabled: callers can still request
`mode=text` for an exact-identifier probe, and every hybrid request still
contains the lexical arm.

The explicit vector and graph modes remain in the scalable assistant fanout.
They are comparatively cheap on the measured corpus and preserve their
arm-specific top results when hybrid's fused result limit would otherwise
truncate them. Local-small and local-advanced defaults are unchanged.

## Persistence and consistency boundaries

- `/state` contains operational graph snapshots, vector data, caches, and
  local state. `/exports` contains reproducible downstream exports.
- SQLite is the single-container backend. PostgreSQL is required when several
  pods serve one knowledge base.
- A knowledge-base shard has one elected indexer/commit authority. Extra
  indexers are failover, not write throughput.
- Graph-service replicas are interchangeable read-only copies of one complete
  shard. Individual edges are not hashed across replicas because paths and
  neighborhoods would become distributed transactions.
- True indexing scale-out uses whole-source knowledge-base shards, each with
  its own state, database scope, indexer, and graph snapshot. Synapse can fan
  retrieval across those shards.
- A remote graph failure makes API readiness fail. APIs never respond by
  materializing the whole graph and multiplying memory pressure during an
  outage.

## Deployment shapes and scaling scope

| Scope | API | Graph | Indexer | Workers | State boundary |
|---|---|---|---|---|---|
| Local/default | one all-role process | in process | in process | local threads/processes | one local `/state` |
| Docker Compose fleet | one published API endpoint by default | one replica by default | one leader; optional standby | four by default | PostgreSQL + NATS + shared volumes |
| Kubernetes shard | HPA on request CPU/latency | scale on graph latency/CPU and availability | StatefulSet, one active writer | KEDA/HPA on queue, in-flight, and preparation backlog | one namespace/release and RWX state per shard |
| Multi-shard | load-balanced/federated | at least one per shard | one authority per shard | independently elastic per shard | separate knowledge bases; Synapse fanout |

Keep the shipped worker, memory, CPU, queue, batching, and replica defaults
until measurements cross a boundary. Scale workers for preparation backlog,
API replicas for request traffic, and graph replicas for graph-query
saturation or availability. If graph save/enrichment or the single ordered
commit dominates, adding any of those replicas cannot help; shard whole
repositories or document collections instead.

The graph-query boundary is retained in the fleet because the measured corpus
reduced API memory and mixed-search wall time. It remains optional for local
profiles, where an HTTP hop and an extra process are unnecessary. See
[the measured decision](how-to/graph-query-service.md) and
[capacity planning](how-to/capacity-planning.md).

## Architecture regression gate

CI shallow-clones sparse portions of Spark, MLflow, VS Code, LangGraph, and
Deep Agents, then deterministically samples 200 real files from each. It runs
a full index, an unchanged incremental index, and vector/graph/hybrid searches
with a 64-dimension stub embedder and no assistant model or provider key. The
job fails on correctness, unexpected incremental embedding work, or committed
wall-time, throughput, search-p95, and peak-RSS guardrails. The JSON report is
uploaded for 30 days so a pull request can be compared with its predecessors.

These are shared-runner regression guardrails, not production capacity
promises. They deliberately test real repository shapes without making CI
depend on LLM availability, embedding quota, or secrets. See
[Architecture regression testing](how-to/architecture-regression.md).

## Design lineage

The fleet follows the useful boundaries described in
[GitHub's code-search architecture](https://github.blog/engineering/architecture-optimization/the-technology-behind-githubs-new-code-search/):
keep indexing separate from serving, make work durable and retryable,
distribute queries over independently replaceable search shards, and keep
serving state immutable between publications. Pheasant does not copy GitHub's
custom trigram index or split individual graph edges: this system also indexes
documents, semantic vectors, and relationship traversals, and current stress
tests show preparation and PostgreSQL lexical ranking—not substring lookup—as
the first bottlenecks.
