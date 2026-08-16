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
| < 5,000 files | 512 MB, 1 CPU | |
| 5,000–25,000 | 1 GB, 2 CPU | |
| 25,000–75,000 | 2 GB, 2–4 CPU | |
| 75,000–150,000 | 4 GB, 4 CPU | |
| 150,000–240,000 | **6 GB, 4 CPU** | The shipped default. |
| 240,000–500,000 | 8–16 GB | Fine in the cloud; `graph.max_nodes` warns. |
| **> 500,000** | **Shard** | One container stops being sensible. |

These are per **region**, and linear — a cloud deployment can go well past the
bottom row on a single node. Sharding is about operational sanity (parallel
indexing, independent re-indexes, blast radius) more than about a hard wall.

!!! note "The shipped Kubernetes manifest allows 6 GB"

    `deploy/kubernetes/deployment.yaml` and the Helm chart limit memory to
    **6 Gi**, which covers roughly **350,000 files** on the table above. It was
    1 Gi (about 65,000 files) until 2026-08-16; a corpus past that was
    OOM-killed mid-sync and it presented as an unexplained restart rather than
    as a capacity problem. Raise it further for a cloud deployment — the
    numbers above are per-region and linear.

## Checkpoint cost, and why it is no longer the ceiling

A checkpoint serializes the entire graph, so its cost grows with the index. The
interval is **derived from that cost**, not fixed — pheasant uses Young's
formula (`T = sqrt(2 x C x MTBF)`), the standard HPC/ML-training result, which
minimizes the *sum* of checkpoint overhead and work redone after a crash rather
than either alone:

| Corpus | Checkpoint cost | Interval | Overhead | Worst-case rework |
|---|---|---|---|---|
| 2,000 files | 0.13 s | 150 s | 0.1% | 2.5 min |
| 20,000 files | 1.3 s | 8 min | 0.3% | 8 min |
| 100,000 files | 6.7 s | 18 min | 0.6% | 18 min |
| 250,000 files | 19.9 s | 31 min | 1.1% | 31 min |

Overhead stays around 1% at every size. The trade is bounded rework — at most
one interval of indexing is lost to a crash — capped by
`storage.graph_checkpoint_max_seconds` (30 min).

```yaml
storage:
  checkpoint_mtbf_seconds: 86400      # expected time between interruptions
  graph_checkpoint_max_seconds: 1800  # ceiling on lost work
```

Lower `checkpoint_mtbf_seconds` on spot or preemptible instances: interruptions
are more frequent there, and the formula responds by checkpointing more often —
which is exactly when that pays for itself.

So checkpoint cost is **not** what limits one region. RAM is, and it grows
linearly — the table at the top is the guide.

pheasant warns once per sync when the graph passes `graph.max_nodes` (default
**1.5M nodes**, ≈240,000 files — the point at which the shipped 6 Gi container
is genuinely full). It is a notice, not a refusal: by the time it can fire the
index already exists, and discarding it would help nobody. Raise it *and* the
container limit together, or shard.

```yaml
graph:
  max_nodes: 1500000   # null disables the warning
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
6.7 s checkpoint each — indexing in parallel, instead of one 10 GB container
indexing serially.

### Let pheasant propose the split

```bash
pheasant shard plan -c pheasant.yaml            # fewest regions that fit
pheasant shard plan -c pheasant.yaml --shards 4 # exactly four
```

It scans each source (a directory walk, not an index), packs whole sources
largest-first, and prints which sources go where with a memory request per
region:

```text
600,000 files across 3 region(s) (~3,780,000 graph nodes)

  shard-1: 220,000 files, ~1,386,000 nodes, ~5.5 GB RSS -> request 8Gi
      - platform-monorepo
  shard-2: 200,000 files, ~1,260,000 nodes, ~5.0 GB RSS -> request 8Gi
      - docs-site
      - runbooks
```

The count defaults to the fewest regions that keep every shard under
`graph.max_nodes`, so the planner and the runtime warning agree about what "too
big" means. A single source over budget on its own is reported rather than
split — no arrangement of whole sources fixes that, and the honest answers are
more memory for that region or a narrower `include`/`exclude`.

### Writing to one knowledge base from several indexers

Separate from sharding, and only on Postgres: the write lease is **per source**
there rather than per state directory, so two indexers can commit two different
sources concurrently. On SQLite the whole-state lease remains, because SQLite
genuinely permits one writer per file — that is an accurate model, not a
limitation to route around.

## Which storage backend

Independent of the graph, and a different question:

| | SQLite (default) | Postgres |
|---|---|---|
| Setup | none | a database to run |
| Writers | **one process per knowledge base** | **one per source** — several indexers at once |
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
