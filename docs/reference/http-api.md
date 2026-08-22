# HTTP API reference

pheasant serves a FastAPI admin/retrieval API on port `8765` (configurable via
`server.port`). When `server.api.openapi` is enabled, interactive docs are
available at `/docs` and the schema at `/openapi.json`.

The routes below are the consolidated surface defined in
`src/pheasant/api/app.py`.

## Health & ops

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. Reports `role`, so a pod can be identified from the response. Stays 200 when the state store is unreachable — restarting a pod does not bring a database back. |
| GET | `/ready` | Readiness probe. Reports the role and what it does (`watcher`, `scheduler`, `drains_queue`, `indexes_locally`), and returns **503** when the state store is unreachable so the replica leaves the Service without being restarted. Deliberately not gated on the index being populated: a replica held unready through a multi-hour first index would take the whole Service down for that time. |
| GET | `/metrics` | Prometheus exposition text — index queue depth, per-source throughput/ETA/stall, search latency, graph size. See [Monitor indexing](../how-to/monitor-indexing.md). |
| POST | `/internal/indexing/prepare` | Opt-in stateless remote preparation worker. Disabled unless `sync.concurrency.remote_worker_enabled`; requires `Authorization: Bearer` matching the environment variable named by `remote_worker_token_env`. Intended for pheasant coordinators, not public clients. |
| POST | `/internal/indexing/prepare-batch` | Several preparation tasks in one request. Same gate and token as above. Honours `deadline_seconds` (or the `X-Pheasant-Deadline-Seconds` header) by stopping between tasks rather than finishing work whose caller has given up, and answers a repeated `idempotency_keys` entry from a bounded cache instead of re-parsing. `408` when the deadline has already passed, `413` over `MAX_PREPARE_BATCH` tasks or the per-file size limit, `422` when a task is unacceptable (the coordinator then prepares it locally). A worker predating this route returns `404`, and the coordinator falls back to the single-task path. |

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
| GET | `/sources` | List configured + runtime sources. Each entry carries `syncing: bool` (a background sync — `wait: false` — is running now) and `sync_error: string \| null` (error from the most recent *background* sync, independent of `last_status`), plus `job` — the running job behind `syncing`, with its phase and counter (see Jobs below). |
| GET | `/sources/types` | Registerable source types — built-ins plus installed connector plugins — each with a `path_role` of `required` or `unused`. |
| POST | `/sources/quick-add` | One-field setup: a path, URL, glob or connector name is detected, named, registered and (by default) synced. Same inference as `pheasant up`. `sync_now` (default `true`) gates syncing at all; `wait` (default `true`) gates whether the response blocks on it — see Sync below. `wait: false` returns `sync_results: []` and `syncing: [names]` immediately; poll `GET /sources` for progress. |
| POST | `/sources/upload` | **multipart.** Upload documents; they land under `/state/uploads/<name>/`, which is registered as an ordinary `document_folder` source and indexed through the normal pipeline — no second ingestion path. Fields: `files` (repeatable), `source_name` (default `uploads`), `sync_now`, `wait`. A second upload into the same name adds to it. One over-sized or empty file is reported in `rejected[]` without losing the rest. |
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
open for — the pheasant UI hit exactly this as a 504 on `/sources/
quick-add` even though the sync went on to succeed server-side. Poll
`GET /sources` (or `/overview`) for `syncing`/`sync_error` to track a
background sync to completion; a source is only ever "unknown" (404) if
it exists in neither `config.sources` nor the state registry — a source
registered by quick-add/`POST /sources` and never written to YAML is
still valid here, resolved through the same state-registry fallback
`SyncEngine._source` uses.

## Jobs (background work)

| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs` | Every job, newest first; running ones sort ahead of finished. `?active=true` for running only. |
| GET | `/jobs/{job_id}` | One job: phase, counter, log tail, terminal outcome, and a `sources[]` breakdown. |
| GET | `/jobs/stream` | Server-sent events, one per job update, primed with current state on connect. |

Every source row (`/sources`, `/overview`) also carries `syncing`, `sync_error`,
`job` — the live job behind the boolean — and `progress`, **this source's own
slice** of that job: phase, counter, observed throughput, ETA, `stalled`, and
the indexed/unchanged counts. Under a `sync_all` the job-level counter is an
aggregate over every source, so `progress` is what tells you which one is
actually behind. See [Monitor indexing](../how-to/monitor-indexing.md).

## Search & retrieval

| Method | Path | Purpose |
|---|---|---|
| POST | `/search` | Search (`mode`: `text` / `graph` / `vector` / `hybrid`). Also takes `source_name`, `source_types`, `exclude_source_types`, `exclude_sources`, `node_types`, `min_score`, `section` and `memory`. Every hit reports `provenance.source_type` — the kind of source it came from. |
| POST | `/relevant-files` | Rank relevant files for a task/query. |
| GET | `/files/summary` | Summarize a file node. |
| GET | `/nodes/content` | Fetch a node's content. |
| GET | `/nodes/explain` | Explain why a node matched / its provenance. |

### The `memory` argument

`POST /search`, `/relevant-files` and `/assistant/chat` all accept `memory`:
one of `"auto"` (default), `"off"`, `"only"`, `"prefer"`, or an object with
`scopes`, `subject`, `current_only`, `as_of`, `max_results`,
`include_rules` and `tiers`.

`include_rules` defaults to `false`: `alias`/`preference`/`exclusion` records
steer ranking but are not themselves returned as passages. Set it true to see
them in results.

Records a later record corrected are excluded automatically — you do not have
to wait for a consolidation pass. Pass `{"current_only": false}` or an `as_of`
instant to see them. Hits that came from memory carry a `memory` block naming
the record, its scope, subject, when it was asserted, and its tier.

`tiers` (`["hot"]` default) reaches records demoted by compaction
(`memory.compaction_enabled`) — `["cold"]` or `["hot","cold"]`; `current_only:
false` and `as_of` widen to both tiers automatically, same as they already
widen the validity window.

## Agent memory

| Method | Path | Purpose |
|---|---|---|
| POST | `/memory/enable` | Provision the `type: memory` source. Idempotent; the only way to turn memory on without editing `pheasant.yaml`. |
| POST | `/memory` | Append one record. Body: `text`, `scope` (`session`/`user`/`org`), `subject`, `supersedes`, `tags`, `kind`, `principal`, `valid_until`, `sync`. Response adds `outcome` (`"created"` \| `"reinforced"` \| `"duplicate"`) and, when a reinforcement changed what is stored, `submitted_text`. |
| GET | `/memory` | List records. Query: `scope`, `current_only`. Each record carries `tier` (`hot`/`cold`) and `subsumed_by`. |
| POST | `/memory/consolidate` | Archive superseded/expired records, prune past `memory.max_records`, then re-index. Returns `{"skipped": …}` when consolidation is off — not an error. |
| POST | `/memory/synthesize` | LLM-merge a near-duplicate cluster deterministic compaction could not resolve into one canonical record, subsuming the inputs. Off by default (`memory.synthesis.enabled`) and never automatic — only this call runs it. `{"skipped": …}` when disabled, no memory source, or no model reachable; otherwise `{"attempted","synthesized","cached","records"}`. |

See [Agent memory](../how-to/agent-memory.md).

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

## Retrieval tuning

| Method | Path | Purpose |
|---|---|---|
| GET | `/assistant/retrieval` | The typed `assistant.retrieval` settings, what is *effective* once `workflow_options` is layered on, and one line of help per knob. |
| PUT | `/assistant/retrieval` | Change one or more knobs. Omitted fields are left alone. Query-time only, so it applies to the next question — no restart, no re-index. |

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
| GET | `/graph/diagnostics` | Structural health: node/edge type histograms, hubs by degree, orphan count, density. Walks the whole graph — do not poll it. |
| GET | `/graph/path` | Shortest path between two nodes (`source`, `target`, `max_depth`). Edges are followed in **both** directions: relatedness is not a question about which way an import points. |

## Filesystem & config

| Method | Path | Purpose |
|---|---|---|
| GET | `/fs/list` | List filesystem entries under allowlisted roots (for the directory picker). |
| GET | `/fs/host-path` | Can pheasant see this host path? Answers `native` / `visible` / `not_mounted` / `unknown`, and for `not_mounted` returns the exact remedy — compose volume, `docker run` flag, and the `allow_workspace_roots` entry it also needs. |
| GET | `/config/sections` | Every config section with whether the running process can pick a change up. |
| PATCH | `/config/section/{section}` | Validate, persist and (where safe) hot-apply **one** section. Reports `applied` vs `restart_required` honestly rather than saying "saved" for a value the process is still ignoring. |
| GET | `/knowledge-base` | This knowledge base's identity and paths. |
| PUT | `/knowledge-base` | Edit name/description. A **rename** changes `kb_id` — the graph root node and every stable artifact id — so it reports the full re-index it implies instead of silently orphaning the graph. |
| GET | `/config` | Current config. |
| GET | `/config/effective` | Resolved config after profile + YAML + overrides. |
| PUT | `/config` | Update config. |

## Example: search

```bash
curl -X POST http://localhost:8765/search \
  -H "content-type: application/json" \
  -d '{"query": "billing owner", "mode": "hybrid", "max_results": 5}'
```
