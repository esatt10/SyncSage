# The role-split fleet

`deploy/kubernetes/` (one Deployment, `role: all`) is the default and needs no
infrastructure. **These manifests are the opposite trade**, and they are worth
it only past the point where one container stops being enough — see
[capacity planning](../../../docs/how-to/capacity-planning.md).

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl apply -f deploy/kubernetes/scaled/
```

## What you must provide first

These are hard requirements, not recommendations. Each one exists because of
something in pheasant's design, so they are stated with the reason:

1. **Postgres** (`storage.backend: postgres`). SQLite permits one writer per
   file, and it is not one file across pods anyway. The DSN is read from the
   env var named by `storage.dsn_env` — put it in a Secret, never in the
   ConfigMap.

2. **A ReadWriteMany volume for `/state`.** The knowledge graph is a file the
   indexer writes and every api replica reads, so RWO (one node) cannot work
   with api replicas spread across nodes. EFS, Azure Files, CephFS and NFS all
   qualify; most default StorageClasses do not. Set `storageClassName` in
   `state-pvc.yaml`.

   API replicas poll that file (`server.api.graph_refresh_seconds`, 30s) and
   reload when it changes. Without the poll they would answer graph queries
   from whatever the graph was when the pod started — text and vector search
   read the database and are always current, which is exactly what makes the
   staleness easy to miss.

3. **The durable queue** (`sync.queue.enabled: true`). An api replica
   publishes index work rather than running it; without a queue it would
   accept syncs that go nowhere, and `--role api` refuses to start.

4. **A metrics adapter**, if you want the worker HPA. Scaling on
   `pheasant_index_queue_depth` needs [prometheus-adapter] or [KEDA] to expose
   it; `worker-hpa.yaml` ships the KEDA form and the CPU fallback.

[prometheus-adapter]: https://github.com/kubernetes-sigs/prometheus-adapter
[KEDA]: https://keda.sh

## The shape

| Workload | Kind | Scales on | Why |
|---|---|---|---|
| `pheasant-api` | Deployment | CPU (HPA) | Request traffic. Stateless — the MCP server is `stateless_http`, so an agent's two requests may land on different replicas. |
| `pheasant-indexer` | StatefulSet, 1 replica | not autoscaled | Owns the watcher and scheduler. **One per shard**, not one per cluster: two indexers on one shard is not faster, it is two processes taking turns. |
| `pheasant-worker` | Deployment | queue depth (HPA/KEDA) | Parse/chunk only. The elastic tier — no state, no credentials, safe to kill. |

Sharding across several knowledge bases means one of these stacks per shard,
each with its own `pheasant.name` and its own database. `pheasant shard plan`
proposes the split.

## What is deliberately not here

* **No HPA on the indexer.** Its work is serialized per source by a lease, so
  a second replica would spend its life losing races. Add indexers by adding
  *shards*.
* **No `PodDisruptionPolicy` allowing the indexer to be evicted freely.** Its
  PDB is `minAvailable: 0` on purpose — one replica cannot satisfy anything
  higher, and pretending otherwise blocks node drains forever. A drained
  indexer's in-flight task is redelivered by the queue's visibility timeout,
  which is what makes that safe.
* **No NetworkPolicy for the worker port.** `deploy/kubernetes/networkpolicy.yaml`
  covers the base install; the preparation endpoints are bearer-authenticated
  and disabled by default, but they should still not be reachable from outside
  the namespace. Extend that policy for your cluster.
