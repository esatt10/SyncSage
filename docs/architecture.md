# Architecture

SyncSage is a Docker-first MCP server with an admin API, source registry, sync engine, parsing pipeline, graph/search stores, and optional Obsidian export.

## Runtime flow

1. Load `/config/syncsage.yaml`.
2. Validate workspace roots, source paths, include/exclude rules, and storage paths.
3. Register enabled sources in the local source registry.
4. Run startup validation and repair missing graph/search state.
5. Start file watchers, git state checks, and scheduled fallback sync.
6. Expose MCP tools/resources/prompts and HTTP health/admin endpoints.
7. Persist graph, manifest, SQLite, export, and Obsidian projection updates.

## Logical components

| Component | Responsibility |
|---|---|
| MCP server | Agent-facing tools, resources, and prompts. |
| Admin API | Health, readiness, source, sync, search, graph, and Obsidian endpoints. |
| Source registry | Configured and runtime-registered source metadata. |
| Sync engine | Connector-backed startup validation, incremental sync, scheduled sync, manual sync, and repair orchestration. |
| Watcher service | Debounced filesystem events for configured paths. |
| Git monitor | Branch, commit, and working tree state detection without mutating repositories. |
| Ingestion pipeline | Repository, Markdown, document, HTML/XML, and web artifact parsing. |
| Graph builder | Stable node/edge upserts into a NetworkX-compatible graph. |
| Search indexer | SQLite FTS/path/hybrid indexing with optional embeddings later. |
| Obsidian exporter | Markdown frontmatter, backlinks, source notes, repo maps, and optional canvas files. |

## Persistence split

- `/state` is the operational source of truth for SQLite, manifests, graph JSON, snapshots, locks, logs, and cache.
- `/vault` is a human-readable projection for Obsidian and should stay concise.
- `/exports` contains graph JSON and visualization payloads that can be regenerated.

## Design constraints

- Keep v0.1 local-first and inspectable.
- Use deterministic parsing, stable IDs, and content hashes for idempotency.
- Do not execute code from indexed repositories.
- Keep Obsidian optional; SyncSage must work without it.
- Prefer one isolated state volume per SyncSage instance.
