# Run graph queries as a separate service

The scalable profile can keep one graph snapshot in a dedicated internal
service instead of one copy in every API and MCP replica. This boundary is
optional: the standalone profiles still load the graph in process and keep the
lower-latency, lower-complexity path for small installations.

## What is separated

The indexer remains the single commit authority for a knowledge-base shard. It
writes an atomically replaced graph snapshot to shared state. A `graph` role
loads and refreshes that snapshot, while API and MCP processes keep only a
bounded query proxy.

```text
API/MCP replicas -- authenticated HTTP --> graph service replica(s)
       |                                      |
       +-- text/vector --> PostgreSQL/Lance   +-- read-only graph snapshot

Indexer --> NATS task --> parse workers --> PostgreSQL/Lance + graph snapshot
```

The internal API exposes query-shaped operations for search, nodes,
neighborhoods, slices, paths, taxonomy, diagnostics and bounded exports. It is
not a write API, and remote clients never fall back to loading the graph. That
failure behavior is deliberate: a graph-service outage makes API readiness
fail instead of causing every API replica to allocate a full snapshot at once.

Service discovery resolves every current HTTP A record and distributes calls
round-robin. Replicas are interchangeable copies of **one shard**. Actual data
sharding remains one complete fleet per knowledge base, with Synapse fanning
search across those knowledge bases. Do not hash individual edges across graph
replicas: path and neighborhood queries would then require a distributed graph
traversal and lose the atomic-snapshot invariant.

## Configure it

Use `pheasant setup`; the generated scalable profile contains:

```yaml
graph:
  query_service_url: http://graph:8765
  query_service_token_env: PHEASANT_GRAPH_SERVICE_TOKEN
  query_service_timeout_seconds: 30
```

The environment variable contains the bearer token, not the YAML. Compose
reuses the generated random worker token as an internal graph token. Kubernetes
expects a separate `PHEASANT_GRAPH_SERVICE_TOKEN` key in
`pheasant-secrets`.

The scalable assistant uses:

```yaml
assistant:
  retrieval:
    retrieval_modes: [vector, graph, hybrid]
```

This does not remove lexical search. Hybrid executes text, vector, and graph
in parallel, so a second `text` fanout repeated the same PostgreSQL ranking
work. The measured high-frequency text median was 18.44 s versus 839 ms for
vector and 598 ms for graph; the duplicate text arm therefore had the weakest
cost/recall case. Explicit `mode=text` remains useful for exact identifiers,
and vector/graph remain explicit in assistant fanout to retain arm-specific
top candidates before hybrid fusion truncates its result set. Local profile
defaults are unchanged.

Start the Compose fleet from the repository root:

```bash
python -m pheasant setup \
  --answers deploy/compose/answers/scalable.json \
  --accept-defaults --plain --target compose \
  --output deploy/compose/fleet.yaml --force

docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml up -d --build \
  --scale indexer=1 --scale graph=1 --scale worker=4
```

The shipped Compose graph tier has a 2.5 GiB limit because an atomic refresh
briefly holds the old and new generations. The API has a 2 GiB limit and no
resident graph. Both tiers probe `/ready`; the API is removed from readiness
when its graph dependency is unavailable, and the graph tier is not ready when
its token is absent.

Scale graph replicas only after measuring graph-query saturation:

```bash
docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml up -d --scale graph=2
```

For Kubernetes, apply `deploy/kubernetes/scaled/graph-deployment.yaml` with the
rest of the scaled manifests. Keep replicas in separate failure domains for
availability. Give each replica enough memory for two graph generations during
refresh.

## 2026-08-26 measured decision

The experiment used PostgreSQL, NATS JetStream, one indexer, four worker
containers, one API and one graph service on the same Docker host. The corpus
contained 10,055 indexed artifacts, about 42,531 chunks, 92,009 graph nodes and
212,270 graph edges across MLflow, Pheasant, Spark, VS Code and the 46-file
Resume document snapshot.

| Measurement | Before: graph in API | After: graph service |
|---|---:|---:|
| Idle API RSS | 1.919 GiB / 3 GiB | 102 MiB cold |
| Idle serving RSS | 1.919 GiB | 671 MiB combined cold |
| Warm stress RSS | 1.919 GiB API | about 1.03 GiB API + 570 MiB graph |
| Same-process graph reload | part of API RSS | 978 MiB transient, 773 MiB settled |
| 40 mixed searches, concurrency 8 | 33.2 s | 20.2-20.9 s |
| Requests completed | 40 / 40 | 40 / 40 |

The API-only steady footprint fell roughly 46% after warm stress, and the cold
combined serving footprint fell roughly 65%. A same-process graph refresh
retained about 203 MiB over the graph tier's cold RSS, but a second controlled
refresh returned to the same 772-773 MiB plateau rather than growing again.
`gc.collect()` plus glibc `malloc_trim(0)` bounds that retained allocator
footprint; the 2.5 GiB limit still leaves ample room for the measured 978 MiB
old-plus-new refresh peak.

The warm mixed-search run improved by 37-39%. This is not proof that an HTTP
hop is intrinsically faster. On this installation it also removed graph work
and graph allocation pressure from the API process, allowing text/vector
fan-out to run with less contention.

A second final harness intentionally used several high-frequency lexical
terms. Its cold and warm totals were 61.7 s and 46.1 s. In the warm run, graph
search was 598 ms median and vector search 839 ms, while text search was 18.44
s and hybrid 6.25 s. All 40 requests still completed. This isolates the
remaining high-fan-out bottleneck in PostgreSQL full-text ranking, not in the
remote graph boundary; moving or replicating edges cannot solve lexical
candidate ranking. Treat the 20.2-20.9 s figure above as the comparable
original workload and keep the high-hit query set as an adversarial capacity
test.

CI also protects this boundary with an offline repository architecture gate.
It deterministically samples Spark, MLflow, VS Code, LangGraph, and Deep Agents;
runs full and unchanged incremental indexing with stub embeddings; executes
vector, graph, and hybrid searches; and fails on correctness or material
throughput, wall-time, search-p95, and memory regressions. See
[Architecture regression testing](architecture-regression.md).

### Incremental and document baselines

The clean full baseline cycle completed all five sources sequentially in 16 m
9 s with no failed source:

| Source | Artifacts | Wall time | Dominant phase |
|---|---:|---:|---|
| MLflow | 3,175 | 281 s | preparing, 179.05 s |
| Pheasant | 338 | 71 s | preparing, 33.13 s |
| Resume documents | 46 | 51 s | saving + committing, 24.67 s |
| Spark | 4,099 | 316 s | preparing, 212.90 s |
| VS Code | 2,392 | 250 s | preparing, 171.97 s |

Large-repository runtime was dominated by parsing/preparation, not graph save
or enrichment. That is the workload the stateless worker tier can help. The
small document source spent almost half its wall time committing and saving
because every source still publishes the shard's whole graph; query replicas
cannot reduce that cost.

The pre-change full incremental pass over all five sources drained in 89.2 s
with zero dead tasks; every source was unchanged. The post-change pass drained
in 136 s of active application time with zero dead tasks and 34 changed VS Code
artifacts. Those runs are not apples-to-apples, so they establish correctness
and stability, not an indexing speedup. The final unchanged Pheasant repository
incremental took 7 s.

The Resume snapshot at
`C:\Users\esatt\OneDrive\Documents\Resume` was copied into the persistent
fleet workspace as `resume-documents-baseline`, avoiding live OneDrive changes
during comparison. Its full 46-document baseline completed in 51 s:

| Phase | Seconds |
|---|---:|
| Listing | 6.19 |
| Preparing/extraction | 10.72 |
| Indexing | 0.39 |
| Enriching | 5.45 |
| Committing | 11.98 |
| Saving | 12.68 |
| Snapshotting | 0.28 |

After the graph-service change, its unchanged incremental completed in 8 s
(6.02 s preparation and 0.11 s saving). This is the expected result: moving
queries does not alter the indexer's commit path.

### Replica scaling result

On this single Docker host, a graph-only 40-request/concurrency-8 run took
13.39 s with one graph replica and 14.84 s with two. Both replicas received
traffic after client-side service discovery was added, but they competed for
the same CPU and PostgreSQL resources. The second replica was therefore
removed and the profile remains at one. Keep dynamic replica discovery for a
multi-node cluster, where another replica can add CPU and availability instead
of contending on one workstation.

## Keep it, scale it, or remove it

Keep the service when at least one of these is true:

- multiple API replicas would duplicate a material graph;
- a 3 GiB API regularly holds roughly 60-70% steady RSS; or
- graph-query traffic needs independent CPU or availability scaling.

This corpus started below the original threshold, but the measured API memory
and mixed-search improvements justify retaining one graph replica in the
scalable profile. The default local profiles are unchanged.

Do **not** add graph replicas to solve slow graph save or enrichment. Those
steps run in the one commit authority and were 18.96 s combined in the changed
VS Code incremental. If they become the dominant part of ordinary runs, use
`pheasant shard plan` and move whole repositories/document collections into
separate knowledge bases. That reduces each snapshot and permits independent
indexers to commit in parallel.

To remove this boundary from a custom fleet, clear `graph.query_service_url`,
remove the graph workload/dependency, restore enough API memory for the graph
and its atomic reload peak, then restart APIs. Never remove the graph service
while clients still reference its URL.

## Verify the running boundary

```bash
curl -fsS http://127.0.0.1:8765/ready
curl -fsS http://127.0.0.1:8765/overview
docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml ps
docker stats --no-stream
```

For an API using the remote boundary, `/ready` reports
`"refreshes_graph": false`; the graph role reports `true`. Stop the sole graph
container during a maintenance test and confirm the API returns 503 from
`/ready`, then returns ready after the graph service recovers.
