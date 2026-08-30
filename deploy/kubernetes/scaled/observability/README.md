# The observation plane and the log tier

Not applied by default, and deliberately in a subdirectory: `kubectl apply -f
../` does not recurse, so the fleet you get from `scaled/` is the fleet it has
always been. This is the Kubernetes equivalent of the `observability` Compose
profile.

Turning it on means the region **records queries and principals**. That is an
operator's decision, never a default — which is why enabling it takes an
explicit edit to the shared ConfigMap rather than just applying a Deployment.

## What it does

Every API and MCP call emits a span. Those spans become rows in an interaction
ledger with a retention policy — never files, never indexed, never returned by
a search. [Memory formation](../../../../docs/memory-formation.md) reads that
ledger to propose memory candidates a person can promote.

The log tier is what keeps all of it off two hot paths:

* **the request** — the API appends to a bounded in-memory ring and publishes
  batches; it never writes a ledger row inline, because that would put a
  database write per search on the same PostgreSQL the lexical search arm
  already contends on;
* **the indexer's `sync_lock`** — rolling expired rows to Parquet happens here,
  not on the scheduler beat, where a multi-million-row write would stall
  incremental sync for every source in the region.

Under pressure the tier drops observations rather than slowing a request.
`pheasant_interaction_events_dropped_total{reason}` counts what was lost.

## Enabling it

**1. Edit `pheasant-fleet-config`** (`../configmap.yaml`) to add:

```yaml
observability:
  interactions:
    enabled: true
    hot_retention_days: 7
    cold_enabled: true          # roll to Parquet under /exports before deleting
    cold_retention_days: null   # null keeps cold partitions forever
    queue:
      enabled: true
      backend: nats
      nats_stream: PHEASANT_LOGS
      nats_subject: pheasant.logs.batches
      nats_durable: pheasant-loggers
```

Both flags are required. `serve --role logger` refuses to start without them
rather than idling while reporting itself healthy — a pod that is green and
doing nothing is the failure this guard exists to prevent.

**2. Roll the API and indexer** so they pick up the new config, then:

```bash
kubectl apply -f deploy/kubernetes/scaled/observability/
```

## Scaling it

On `pheasant_log_queue_depth`, never on the index queue's depth: the two rise
for different reasons — this one with request traffic, that one with corpus
churn — and they are different tables with different failure modes.

One replica handles a great deal. A batch is 500 observations by default and
the work per batch is a multi-row insert, so this tier scales far later than
preparation does. The included `ScaledObject` scales to zero, because a region
with no traffic has no observations.

## Cold storage enforces nothing

Partitions land under `/exports/interactions/dt=YYYY-MM-DD/`. A Parquet
directory has no access control, and these rows carry principals and query
text. Put the access control on the volume, exactly as
[the export schema](../../../../docs/reference/export-schema.md) says of
exports.

To keep the audit trail and the evaluation corpus **without** a query-time
ledger, set `hot_retention_days: 0`: batches then go straight to Parquet and
`/state` never grows at all.
