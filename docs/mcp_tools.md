# MCP Tools, Resources, and Prompts

MCP is the primary agent interface. Tool responses should be compact, ranked, and provenance-rich.

## Server transports

pheasant exposes MCP through the official Python MCP SDK when the `mcp` extra is installed. The Docker image includes this runtime.

```bash
pheasant mcp --config /config/pheasant.yaml --transport stdio
```

For VS Code, keep pheasant running with Docker Compose and let VS Code start the MCP protocol process inside that container:

```bash
pheasant compose-env pheasant.yaml --output .pheasant/compose.env
docker compose --env-file .pheasant/compose.env up -d
docker exec -i pheasant python -m pheasant mcp --config /config/pheasant.yaml --transport stdio
```

The command is intended to be owned by the MCP client, so it waits on stdio. Do not add Docker's `-d` detach flag to the MCP stdio command.

## VS Code client config

Create `.vscode/mcp.json` locally from the reusable template:

```bash
mkdir -p .vscode
cp examples/vscode/mcp.json .vscode/mcp.json
```

Or generate it from the pheasant CLI:

```bash
pheasant client-config vscode --output .vscode/mcp.json
```

The committed template contains no host-specific paths. `.vscode/mcp.json` is ignored because users often customize container names, images, volumes, or local environment values.

## Tools

!!! warning "Removed in 0.10.0: `export_obsidian_notes`"

    The Obsidian vault projection was removed, and with it the
    `export_obsidian_notes` tool and the `POST /obsidian/export` endpoint. The
    UI's graph workspace (`/graph`) covers what the vault was used for.

    This is a **breaking change to the MCP tool surface**, which is otherwise
    evolved additively — an agent still calling `export_obsidian_notes` will
    get an unknown-tool error rather than a deprecation warning. It was
    removed outright rather than deprecated because, with the exporter gone,
    there is nothing left for the tool to do.

    Indexing an Obsidian vault as a **source** (`type: obsidian_vault`) is
    unaffected and remains fully supported.

| Tool | Purpose |
|---|---|
| `list_knowledge_bases` | Return registered knowledge bases and status. |
| `register_source` | Add a source at runtime after path/include/exclude validation. Optional `sync_now`; `wait=false` returns a followable background job. |
| `start_sync_source` | Start one source sync and immediately return a job id. |
| `get_job` | Read one background job's phase, counters, log tail and terminal result/error. |
| `list_jobs` | List recent jobs, optionally active jobs only. |
| `list_sources` | List sources with filters, status, and pagination. |
| `disable_source` | Disable a source without deleting its indexed state. |
| `remove_source` | Remove a source and its indexed state. |
| `promote_runtime_source_to_config` | Return a deterministic YAML patch, or write one by policy, for runtime sources. |
| `sync_source` | Trigger `incremental`, `full`, `validate_only`, or `repair` sync for one source. |
| `sync_all` | Trigger sync for all enabled sources. |
| `memory_write` | Append one agent-memory record (`session`/`user`/`org` scope, optional `subject`/`supersedes`/`tags`) to the configured `type: memory` source and, by default, index it immediately — the memory is retrievable via `search_context` in the same session. Recall is ordinary search; there is no separate read path. |
| `memory_consolidate` | Run one consolidation pass now: archive superseded and TTL-expired memory records (files renamed `.md.archived`, never deleted) and re-sync the memory source so they leave the index. The scheduler runs this automatically; this is the on-demand edge. |
| `search_context` | Search graph/search state in `text` (SQLite full-text over chunk content and paths), `graph` (node/relationship labels, types and attribute values), `vector` (embedding similarity; requires `search.embeddings.enabled`, otherwise contributes nothing), or `hybrid` (merged and re-ranked) mode. Also accepts **retrieval criteria** an agent can set per call instead of relying on how the region was configured: `source_name`, `exclude_sources`, `node_types`, `min_score`. All optional and additive — an existing caller is unaffected. |
| `describe_retrieval` | Report how this knowledge base retrieves and what an agent may override per call: default mode and result count, which modes actually work here (`vector` is only offered when a vector index exists), the sources present, the `assistant.retrieval` settings, and one line of help per knob. Call this before guessing at parameters for an unfamiliar region. |
| `preview_retrieval` | Run retrieval criteria and report how they differ from the standing configuration — both result sets plus the delta (added / dropped / kept). Lets an agent test a setting against real content before anyone writes it into `pheasant.yaml`. Read-only: nothing is persisted. |
| `get_relevant_files` | Return files likely needed for a coding task. |
| `get_graph_neighbors` | Traverse graph neighbors with true depth-aware BFS and optional edge-type filters. |
| `get_file_summary` | Return a compact summary and provenance for a file. |
| `get_repo_map` | Return repository structure, important modules, and dependencies. |
| `explain_node` | Explain a graph node and why it matters. |
| `get_sync_status` | Return queue, lock, error, freshness, and connector checkpoint status. |
| `get_sync_history` | Return runtime registration, sync, promotion, disable, and removal audit events. |

### Agent memory in retrieval

`search_context` and `preview_retrieval` take a `memory` argument: one of
`"auto"` (default), `"off"`, `"only"`, `"prefer"`, or an object with
`scopes` / `subject` / `current_only` / `as_of` / `max_results` /
`include_rules` (default `false` — steering records steer ranking but are not
returned as passages). Records a
later record corrected are excluded automatically — pass an `as_of` instant to
ask what was believed then. Hits that came from memory carry a `memory` block
naming the record, its scope and when it was asserted.

`memory_write` takes `kind` (`fact` by default; `alias` / `preference` /
`exclusion` are retrieval rules), `principal` (who asserted it — part of the
record id, and what scopes it under `security.acl_enforced`) and `valid_until`.

`describe_retrieval` reports the memory source's name, its scopes and counts,
how many records are wired into the graph, and any steering in force, so an
agent never has to guess the source name to exclude it.

## Resources

```text
pheasant://knowledge-bases
pheasant://knowledge-bases/{kb_id}/sources
pheasant://knowledge-bases/{kb_id}/graph
pheasant://knowledge-bases/{kb_id}/sources/{source_id}/manifest
pheasant://knowledge-bases/{kb_id}/sources/{source_id}/repo-map
pheasant://knowledge-bases/{kb_id}/sources/{source_id}/history
pheasant://knowledge-bases/{kb_id}/sync-history
pheasant://knowledge-bases/{kb_id}/graph-slices/{node_id}
pheasant://knowledge-bases/{kb_id}/nodes/{node_id}
```

## Prompts

### `use_pheasant_for_coding_task`

1. Call `get_relevant_files` with the user's task.
2. Inspect returned files/chunks.
3. Make the smallest safe change.
4. Run checks.
5. Commit or record the write action.
6. Call `sync_source` with `mode=incremental`.
7. Check `get_sync_status` before the next task.

### `use_pheasant_for_document_research`

Use `search_context` first, prefer chunks with explicit provenance, avoid claims beyond retrieved evidence, and call `get_graph_neighbors` for related material.
