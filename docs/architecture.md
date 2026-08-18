# Architecture

pheasant is a Docker-first MCP server with an admin API, source registry, sync engine, parsing pipeline, and graph/search stores.

## Runtime flow

1. Load `/config/pheasant.yaml`.
2. Validate workspace roots, source paths, include/exclude rules, and storage paths.
3. Register enabled sources in the local source registry.
4. Run startup validation and repair missing graph/search state.
5. Start file watchers, git state checks, and scheduled fallback sync.
6. Expose MCP tools/resources/prompts and HTTP health/admin endpoints.
7. Persist graph, manifest, SQLite, and export updates.

## Logical components

| Component | Responsibility |
|---|---|
| MCP server | Agent-facing tools, resources, and prompts. |
| Admin API | Health, readiness, source, sync, search, and graph endpoints. |
| Source registry | Configured and runtime-registered source metadata, lifecycle state, and audit history. |
| Sync engine | Connector-backed startup validation, incremental sync, scheduled sync, manual sync, and repair orchestration. It pipelines discovery → bounded immutable preparation → bounded batched embedding → ordered commits → global enrichment/finalization. |
| Watcher service | Debounced filesystem events for configured paths. |
| Git monitor | Branch, commit, and working tree state detection without mutating repositories. |
| Ingestion pipeline | Repository, Markdown, document, HTML/XML, and web artifact parsing. |
| Graph builder | Stable node/edge upserts plus code, document, and similarity enrichment. |
| Search indexer | SQLite FTS/path/hybrid indexing with graph-term expansion and optional vector embeddings (shipped — `search.embeddings`/`search.vector_store`, `numpy` or `lancedb`, `mode="vector"`/`"hybrid"`). |

## Persistence split

- `/state` is the operational source of truth for SQLite, manifests, graph JSON, snapshots, locks, logs, and cache.
- `/exports` contains graph JSON and visualization payloads that can be regenerated.

## Design constraints

- Keep v0.1 local-first and inspectable.
- Use deterministic parsing, stable IDs, and content hashes for idempotency.
- Do not execute code from indexed repositories.
- Prefer one isolated state volume per pheasant instance.

## Indexing concurrency

Discovery establishes a stable item order without reading every file twice.
File workers read and SHA-256 independent items, skip unchanged content before
parsing, then parse changed items without mutating operational state. Prepared artifacts
are consumed in discovery order; SQLite rows, graph mutations, manifests and
vector-store upserts pass through one coordinated writer. Embedding HTTP calls
may overlap in provider-sized batches, but their results are reassembled in
input order before one upsert. The persisted node-link graph sorts nodes and
semantic edge payloads, so executor scheduling cannot change its bytes.

Local `thread` workers suit connector/file I/O. Local `process` workers move
plain-text parsing onto separate interpreters and size themselves from the CPU
quota visible to the process. In cluster mode, `remote` workers accept the same
immutable preparation task over an authenticated HTTP protocol. Remote workers
are stateless: they do not mount or mutate the coordinator's `/state`; all
authoritative commits remain centralized.
