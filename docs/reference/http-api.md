# HTTP API reference

SyncSage serves a FastAPI admin/retrieval API on port `8765` (configurable via
`server.port`). When `server.api.openapi` is enabled, interactive docs are
available at `/docs` and the schema at `/openapi.json`.

The routes below are the consolidated surface defined in
`src/syncsage/api/app.py`.

## Health & ops

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. |
| GET | `/ready` | Readiness probe. |
| GET | `/metrics` | Operational metrics. |

## Synapse region

| Method | Path | Purpose |
|---|---|---|
| GET | `/contract` | The region's published semantic contract (read-only). |

See [Attach to a Synapse fleet](../how-to/attach-to-synapse.md).

## Knowledge bases & sources

| Method | Path | Purpose |
|---|---|---|
| GET | `/knowledge-bases` | List knowledge bases. |
| GET | `/sources` | List configured + runtime sources. |
| POST | `/sources` | Register a runtime source. |
| PUT | `/sources/{source_id}` | Update a source. |
| POST | `/sources/{source_id}/disable` | Disable a source. |
| DELETE | `/sources/{source_id}` | Remove a source. |
| POST | `/sources/{source_id}/promote` | Promote a runtime source to durable config. |
| GET | `/sources/{source_id}/repo-map` | Repository map for a source. |
| GET | `/sources/{source_id}/history` | Source sync/audit history. |

## Sync

| Method | Path | Purpose |
|---|---|---|
| POST | `/sync` | Sync all sources (mode in body). |
| POST | `/sync/{source_id}` | Sync one source. |
| GET | `/sync/status` | Current sync status. |

## Search & retrieval

| Method | Path | Purpose |
|---|---|---|
| POST | `/search` | Search (`mode`: `text` / `graph` / `vector` / `hybrid`). |
| POST | `/relevant-files` | Rank relevant files for a task/query. |
| GET | `/files/summary` | Summarize a file node. |
| GET | `/nodes/content` | Fetch a node's content. |
| GET | `/nodes/explain` | Explain why a node matched / its provenance. |

## Graph

| Method | Path | Purpose |
|---|---|---|
| GET | `/graph` | Full graph. |
| GET | `/graph/slice` | Subgraph around a node. |
| GET | `/graph/neighbors` | Neighbors of a node (depth + edge filters). |
| GET | `/graph/export/node-link-json` | Export graph as node-link JSON. |
| GET | `/graph/export/cytoscape-json` | Export graph as Cytoscape JSON. |

## Filesystem & config

| Method | Path | Purpose |
|---|---|---|
| GET | `/fs/list` | List filesystem entries under allowlisted roots (for the directory picker). |
| GET | `/config` | Current config. |
| GET | `/config/effective` | Resolved config after profile + YAML + overrides. |
| PUT | `/config` | Update config. |

## Obsidian

| Method | Path | Purpose |
|---|---|---|
| POST | `/obsidian/export` | Export (or preview, with `{"preview": true}`) the Obsidian vault projection. |

## Example: search

```bash
curl -X POST http://localhost:8765/search \
  -H "content-type: application/json" \
  -d '{"query": "billing owner", "mode": "hybrid", "max_results": 5}'
```

## Example: preview an Obsidian export

```bash
curl -X POST http://localhost:8765/obsidian/export \
  -H "content-type: application/json" \
  -d '{"preview": true, "template_profile": "engineering"}'
```
