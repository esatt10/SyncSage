# How to back up and restore region state

`/state` is SyncSage's operational source of truth — SQLite, the graph and its
snapshots, manifests, the published contract, the event stream, and the vector
index. Treat it as **user data** and back it up.

## What's in a backup

`syncsage backup` writes a single `.tar.zst` archive containing:

| Item | Notes |
|---|---|
| `syncsage.db` | A **consistent** SQLite snapshot via `VACUUM INTO` (never a raw copy; survives WAL). |
| `graphs/` | `graph.latest.json` plus all `graph.<ts>.json.zst` snapshots. |
| `contract.latest.json` | The published Synapse contract, when present. |
| `events/` | The append-only NDJSON sync-event stream, when present. |
| `vectors/` | The per-region vector index, when present. |

The archive is written durably (temp file + fsync + atomic rename).

## Create a backup

```bash
syncsage backup ./backups/syncsage-$(date -u +%Y%m%dT%H%M%SZ).tar.zst \
  --config syncsage.yaml
```

The first positional argument is the output path; `--config` / `-c` points at
your config so the correct `/state` is located.

## Restore a backup

```bash
syncsage restore ./backups/syncsage-20260621T120000Z.tar.zst \
  --config syncsage.yaml
```

Restore is **safe by default**:

- It refuses a non-empty target unless you pass `--force`.
- Extraction is protected against path traversal.
- The DB is validated (`PRAGMA integrity_check`) before being swapped in.
- The swap is atomic; the previous state is preserved as
  `<name>.replaced-<ts>` — never deleted.

To overwrite an existing populated state:

```bash
syncsage restore ./backups/syncsage-20260621T120000Z.tar.zst \
  --config syncsage.yaml --force
```

## Validate after restore

After a restore, confirm the region is healthy and consistent:

```bash
# config + environment
syncsage validate syncsage.yaml
syncsage doctor --config syncsage.yaml

# rebuild any missing/invalid derived state from manifests + DB
syncsage repair --config syncsage.yaml

# runtime health
syncsage start --config syncsage.yaml &
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

If a source looks stale or partially indexed, run a targeted repair or a full
sync:

```bash
syncsage sync --config syncsage.yaml --source <name> --mode repair
syncsage sync --config syncsage.yaml --source <name> --mode full
```

## Graph snapshots and retention

Independently of explicit backups, SyncSage writes zstd-compressed, timestamped
graph snapshots after a successful sync
(`graphs/<kb_id>/graph.<utc-ts>.json.zst`), beside the uncompressed
`graph.latest.json`. They are:

- **Throttled** by `storage.graph_snapshot_interval_seconds` (default 900s).
- **Bounded** by `storage.max_state_size_gb` — when the cap is exceeded, the
  oldest snapshots are evicted first; `graph.latest.json`, the SQLite DB, and
  the contract are **never** evicted.

```yaml
storage:
  graph_snapshots: true
  graph_snapshot_interval_seconds: 900
  compression: zstd
  max_state_size_gb: 10
```

Snapshots are point-in-time history; `syncsage backup` is the portable archive
you copy off-box.

## Operational notes

- Never point two independent SyncSage instances at the same writable `/state`.
  Use one volume per instance.
- Back up before upgrades or migrations. State migrations are one-shot and
  idempotent and preserve originals, but a fresh archive is cheap insurance.
