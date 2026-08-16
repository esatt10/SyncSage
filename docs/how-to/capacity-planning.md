# How big can one region get?

Every scaling decision in pheasant comes back to one number: **how many nodes
the knowledge graph has**. The graph is held in memory during a sync and
written out as one compressed blob, so it sets the container's RAM, the time
each checkpoint costs, and how long a restart takes before it can serve.

The numbers below are **measured, not modelled** — `python -m
pheasant.graph.capacity` builds a real `SimpleMultiDiGraph` with realistic
attribute payloads at each scale and reports what it cost. Re-run it on your
own hardware; the shape will hold, the constants will move.

## Measured cost

Four scale points, on one container. Node and edge counts come from the ratio a
real corpus produces (~6.3 nodes and ~6.3 edges per file, taken from the live
2,132-file demo corpus at 13,503 nodes / 13,502 edges).

| Corpus | Nodes | Process RSS | Checkpoint write | Restart load | Edge scan |
|---|---|---|---|---|---|
| 2,000 files | 12.6k | **34 MB** | 0.13 s | 0.10 s | 1.8 ms |
| 20,000 files | 126k | **314 MB** | 1.3 s | 0.9 s | 16 ms |
| 100,000 files | 630k | **1.5 GB** | 6.7 s | 4.6 s | 95 ms |
| 250,000 files | 1.58M | **4.3 GB** | 19.9 s | 11.2 s | 240 ms |

Cost per node is flat across all four (~2.4 KB of RSS), so this is linear and
you can interpolate. Do **not** extrapolate far past the top row: a linear
projection to 250k files predicted 3.8 GB and the measured answer was 4.3 GB,
12% high.

## Sizing a region

| Corpus | Give the container | Notes |
|---|---|---|
| < 5,000 files | 512 MB, 1 CPU | The shipped defaults are fine. |
| 5,000–25,000 | 1 GB, 2 CPU | |
| 25,000–75,000 | 2 GB, 2–4 CPU | |
| 75,000–120,000 | 4 GB, 4 CPU | Approaching the practical ceiling. |
| **> 120,000** | **Shard instead** | See below. |

!!! warning "The shipped Kubernetes manifest is 1 GB"

    `deploy/kubernetes/deployment.yaml` and the Helm chart both request a
    **1 Gi** limit. On the table above that covers roughly **65,000 files**.
    Past that the pod is OOM-killed mid-sync, which looks like an unexplained
    restart rather than a capacity problem. Raise the limit or shard.

## The real ceiling is checkpoint time, not RAM

RAM is the obvious constraint and it is not the binding one. `storage.
graph_checkpoint_seconds` defaults to **60 s**, and a checkpoint has to
serialize the entire graph:

| Corpus | Checkpoint write | Share of a 60 s interval |
|---|---|---|
| 20,000 files | 1.3 s | 2% |
| 100,000 files | 6.7 s | 11% |
| 250,000 files | 19.9 s | **33%** |
| ~500,000 files | ~40 s (projected) | **67%** |

A third of a long sync spent writing the graph out is already poor; past
roughly 500,000 files the write approaches the interval and the sync spends
most of its time serializing. **That, not memory exhaustion, is why one region
stops being the right answer** — and it is why raising RAM alone does not buy
you much.

pheasant warns about this once per sync via `graph.max_nodes` (default
**750,000 nodes**, ≈120,000 files). It is a notice, not a refusal: by the time
it can fire the index already exists, and discarding it would help nobody.

```yaml
graph:
  max_nodes: 750000   # null disables the warning
```

## When you cross the line: shard

Split the corpus across several pheasant regions and let the
[Synapse router](attach-to-synapse.md) fan out across them. Each region is an
ordinary container with its own `/state`, so:

* memory and checkpoint cost divide by the number of shards, and both are the
  constraints above;
* regions index **in parallel**, so wall-clock time to first index divides too;
* a region can be re-indexed or replaced without touching the others.

Shard along a boundary that matches how people search — per repository, per
team, per document collection — rather than by hashing paths. Retrieval quality
depends on related content living in the same graph: splitting one repository
across two regions breaks the cross-source edges that make
`get_graph_neighbors` useful, while splitting *between* repositories costs
nothing, because those edges were never going to exist.

Sizing a fleet is the single-region table divided by the shard count. 600,000
files across 6 regions is six containers of the 100,000-file row — 1.5 GB and a
6.7 s checkpoint each — not one impossible container.

## Which storage backend

Independent of the graph, and a different question:

| | SQLite (default) | Postgres |
|---|---|---|
| Setup | none | a database to run |
| Writers | **one process per knowledge base** | many (Phase 35.4) |
| Read replicas | one container | many |
| Ranking | BM25 | see [configuration](../configuration.md#where-state-lives-storagebackend) |

Use SQLite unless you need several processes writing one knowledge base, or
several replicas serving it. It is not slower for a single container, and it is
one fewer thing to operate. Postgres is what lifts the one-writer limit; it does
**not** change the graph numbers above, because the graph is held in memory
either way.

## Measure your own

```bash
python -m pheasant.graph.capacity --files 2000,20000,100000
python -m pheasant.sync.benchmark --workers 1,2,4,8
pheasant scan -c pheasant.yaml       # file count and size before indexing
```

`pheasant scan` walks a source without reading it, so you can check a corpus
against the tables above **before** committing to a first index.

## Related

- [Monitor indexing](monitor-indexing.md) — throughput, ETA and stall detection.
- [Speed up indexing](indexing-performance.md) — worker counts and executors.
- [Attach to a Synapse fleet](attach-to-synapse.md) — running several regions.
