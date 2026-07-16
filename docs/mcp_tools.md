# MCP Tools, Resources, and Prompts

MCP is the primary agent interface. Tool responses should be compact, ranked, and provenance-rich.

## Server transports

SyncSage exposes MCP through the official Python MCP SDK when the `mcp` extra is installed. The Docker image includes this runtime.

```bash
syncsage mcp --config /config/syncsage.yaml --transport stdio
```

For VS Code, keep SyncSage running with Docker Compose and let VS Code start the MCP protocol process inside that container:

```bash
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

The command is intended to be owned by the MCP client, so it waits on stdio. Do not add Docker's `-d` detach flag to the MCP stdio command.

## VS Code client config

Create `.vscode/mcp.json` locally from the reusable template:

```bash
mkdir -p .vscode
cp examples/vscode/mcp.json .vscode/mcp.json
```

Or generate it from the SyncSage CLI:

```bash
syncsage client-config vscode --output .vscode/mcp.json
```

The committed template contains no host-specific paths. `.vscode/mcp.json` is ignored because users often customize container names, images, volumes, or local environment values.

## Tools

| Tool | Purpose |
|---|---|
| `list_knowledge_bases` | Return registered knowledge bases and status. |
| `register_source` | Add a source at runtime after path/include/exclude validation. |
| `list_sources` | List sources with filters, status, and pagination. |
| `disable_source` | Disable a source without deleting its indexed state. |
| `remove_source` | Remove a source and its indexed state. |
| `promote_runtime_source_to_config` | Return a deterministic YAML patch, or write one by policy, for runtime sources. |
| `sync_source` | Trigger `incremental`, `full`, `validate_only`, or `repair` sync for one source. |
| `sync_all` | Trigger sync for all enabled sources. |
| `memory_write` | Append one agent-memory record (`session`/`user`/`org` scope, optional `subject`/`supersedes`/`tags`) to the configured `type: memory` source and, by default, index it immediately — the memory is retrievable via `search_context` in the same session. Recall is ordinary search; there is no separate read path. |
| `search_context` | Search graph/search state in `text` (SQLite full-text over chunk content and paths), `graph` (node/relationship labels, types and attribute values), `vector` (embedding similarity; requires `search.embeddings.enabled`, otherwise contributes nothing), or `hybrid` (merged and re-ranked) mode. |
| `get_relevant_files` | Return files likely needed for a coding task. |
| `get_graph_neighbors` | Traverse graph neighbors with true depth-aware BFS and optional edge-type filters. |
| `get_file_summary` | Return a compact summary and provenance for a file. |
| `get_repo_map` | Return repository structure, important modules, and dependencies. |
| `explain_node` | Explain a graph node and why it matters. |
| `export_obsidian_notes` | Preview or write Obsidian notes for a knowledge base or source. |
| `get_sync_status` | Return queue, lock, error, freshness, and connector checkpoint status. |
| `get_sync_history` | Return runtime registration, sync, promotion, disable, and removal audit events. |

## Resources

```text
syncsage://knowledge-bases
syncsage://knowledge-bases/{kb_id}/sources
syncsage://knowledge-bases/{kb_id}/graph
syncsage://knowledge-bases/{kb_id}/sources/{source_id}/manifest
syncsage://knowledge-bases/{kb_id}/sources/{source_id}/repo-map
syncsage://knowledge-bases/{kb_id}/sources/{source_id}/history
syncsage://knowledge-bases/{kb_id}/sync-history
syncsage://knowledge-bases/{kb_id}/graph-slices/{node_id}
syncsage://knowledge-bases/{kb_id}/nodes/{node_id}
```

## Prompts

### `use_syncsage_for_coding_task`

1. Call `get_relevant_files` with the user's task.
2. Inspect returned files/chunks.
3. Make the smallest safe change.
4. Run checks.
5. Commit or record the write action.
6. Call `sync_source` with `mode=incremental`.
7. Check `get_sync_status` before the next task.

### `use_syncsage_for_document_research`

Use `search_context` first, prefer chunks with explicit provenance, avoid claims beyond retrieved evidence, and call `get_graph_neighbors` for related material.
