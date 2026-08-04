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
| GET | `/overview` | One call for a UI cold start: knowledge base, sources, node counts, whether anything is indexed. Each source in `sources[]` carries live `syncing`/`sync_error` (see Sync below). |
| GET | `/sources` | List configured + runtime sources. Each entry carries `syncing: bool` (a background sync — `wait: false` — is running now) and `sync_error: string \| null` (error from the most recent *background* sync, independent of `last_status`). |
| GET | `/sources/types` | Registerable source types — built-ins plus installed connector plugins — each with a `path_role` of `required` or `unused`. |
| POST | `/sources/quick-add` | One-field setup: a path, URL, glob or connector name is detected, named, registered and (by default) synced. Same inference as `syncsage up`. `sync_now` (default `true`) gates syncing at all; `wait` (default `true`) gates whether the response blocks on it — see Sync below. `wait: false` returns `sync_results: []` and `syncing: [names]` immediately; poll `GET /sources` for progress. |
| POST | `/sources` | Register a runtime source (full schema). Accepts plugin types; a plugin source needs no local path. Same `sync_now`/`wait` fields as quick-add. |
| PUT | `/sources/{source_id}` | Update a source. |
| POST | `/sources/{source_id}/disable` | Disable a source. |
| DELETE | `/sources/{source_id}` | Remove a source. |
| POST | `/sources/{source_id}/promote` | Promote a runtime source to durable config. |
| GET | `/sources/{source_id}/repo-map` | Repository map for a source. |
| GET | `/sources/{source_id}/history` | Source sync/audit history. |

## Sync

| Method | Path | Purpose |
|---|---|---|
| POST | `/sync` | Sync all sources (`mode` in body). |
| POST | `/sync/{source_id}` | Sync one source. |
| GET | `/sync/status` | Current sync status. |

`/sync` and `/sync/{source_id}` both accept `wait` in the JSON body
(default `true`, preserving the original blocking contract: the request
holds open until the sync finishes and returns its full
indexed/skipped/graph counts). Set `wait: false` to return immediately —
`{"status": "syncing", ...}` — while the sync runs in a background thread
(the same `sync/worker.py` subprocess path `wait: true` uses; nothing
about *how* the sync runs changes, only whether the caller waits for it).
This exists because a large source's first sync (clone + full index) can
run well past what a browser tab or reverse proxy will hold a connection
open for — the SyncSage UI hit exactly this as a 504 on `/sources/
quick-add` even though the sync went on to succeed server-side. Poll
`GET /sources` (or `/overview`) for `syncing`/`sync_error` to track a
background sync to completion; a source is only ever "unknown" (404) if
it exists in neither `config.sources` nor the state registry — a source
registered by quick-add/`POST /sources` and never written to YAML is
still valid here, resolved through the same state-registry fallback
`SyncEngine._source` uses.

## Search & retrieval

| Method | Path | Purpose |
|---|---|---|
| POST | `/search` | Search (`mode`: `text` / `graph` / `vector` / `hybrid`). |
| POST | `/relevant-files` | Rank relevant files for a task/query. |
| GET | `/files/summary` | Summarize a file node. |
| GET | `/nodes/content` | Fetch a node's content. |
| GET | `/nodes/explain` | Explain why a node matched / its provenance. |

## Assistant (grounded chat)

| Method | Path | Purpose |
|---|---|---|
| GET | `/assistant/status` | Whether a model is reachable, from where (config env var or session key), and the resolved provider/model. |
| POST | `/assistant/key` | Hand the server an API key for this session only. Held in process memory behind an opaque token; never written to config, `/state`, or logs. |
| DELETE | `/assistant/key` | Revoke a session key immediately. |
| GET | `/assistant/workflows` | Available answering workflows, which one `auto` currently resolves to, whether the `[agent]` extra is installed, and each workflow's option defaults. |
| POST | `/assistant/chat` | Ask a question. Returns the answer, numbered citations, graph facts, the nodes to focus, and the workflow's step trace. Accepts `workflow` and `options` overrides. |

See [Ask your knowledge base](../how-to/chat-and-ui.md) and
[Customize the answering workflow](../how-to/agent-workflows.md).

## Semantic search (embeddings)

| Method | Path | Purpose |
|---|---|---|
| GET | `/search/embeddings` | Embeddings settings, vector coverage, and which vector backends are installed here. |
| PUT | `/search/embeddings` | Enable/configure embeddings in the live process; `persist: true` also writes the `search.embeddings` / `search.vector_store` keys back to the config file. Refuses a backend whose optional extra is missing. |
| POST | `/search/embeddings/reindex` | Embed already-indexed content without re-reading sources. Idempotent; `?drop_existing=true` discards vectors left in a stale embedding space. |

See [Vector self-search](../how-to/vector-search.md).

## MCP

| Method | Path | Purpose |
|---|---|---|
| GET | `/mcp/info` | Transports, a ready-to-paste client config, and the tool list an attached agent gets. |

## Graph

| Method | Path | Purpose |
|---|---|---|
| GET | `/graph` | Full graph. Filter with `types` / `exclude_types` / `source` before the node limit applies. |
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
