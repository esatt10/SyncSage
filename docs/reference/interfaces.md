# Interface matrix

SyncSage exposes the same capabilities across several surfaces: the **CLI**, the
**HTTP API**, the **web UI**, and **MCP** (for agents). This page maps each
capability area to the concrete command, route, or tool on each surface so you
can pick whichever fits your workflow.

Legend: — means "not offered on this surface"; use one of the others.

## Configuration

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Generate starter config | `syncsage init --profile <p>` | — | — | — |
| Validate config | `syncsage validate <file>` | — | Config editor (diff preview) | — |
| Show resolved config | `syncsage config show --effective` | `GET /config`, `GET /config/effective` | Config editor | — |
| Edit config | (edit YAML) | `PUT /config` | Config editor (form + raw YAML) | — |
| Environment doctor | `syncsage doctor` | `GET /health`, `GET /ready` | — | — |
| Generate compose env | `syncsage compose-env <file>` | — | — | — |
| Generate VS Code MCP config | `syncsage client-config vscode` | — | — | — |

## Exploration (sources & sync)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| List knowledge bases | — | `GET /knowledge-bases` | — | `list_knowledge_bases` |
| List sources | — | `GET /sources` | Source manager | `list_sources` |
| Register a source | (edit YAML) | `POST /sources` | Add-a-local-directory | `register_source` |
| Update a source | (edit YAML) | `PUT /sources/{id}` | Source manager | — |
| Disable a source | (edit YAML) | `POST /sources/{id}/disable` | Source manager | `disable_source` |
| Remove a source | (edit YAML) | `DELETE /sources/{id}` | Source manager | `remove_source` |
| Promote runtime source to config | — | `POST /sources/{id}/promote` | — | `promote_runtime_source_to_config` |
| Sync one source | `syncsage sync --source <name>` | `POST /sync/{id}` | Source manager | `sync_source` |
| Sync all sources | `syncsage sync --all` | `POST /sync` | Source manager | `sync_all` |
| Repair state | `syncsage repair`, `syncsage sync --mode repair` | `POST /sync` (mode) | — | — |
| Sync status | — | `GET /sync/status` | Source manager | `get_sync_status` |
| Sync history | — | `GET /sources/{id}/history` | — | `get_sync_history` |

## Retrieval (search & graph)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Search (text/graph/vector/hybrid) | — | `POST /search` | Search box | `search_context` |
| Relevant files for a task | — | `POST /relevant-files` | — | `get_relevant_files` |
| File summary | — | `GET /files/summary` | Node inspector | `get_file_summary` |
| Repo map | — | `GET /sources/{id}/repo-map` | — | `get_repo_map` |
| Node content | — | `GET /nodes/content` | Node inspector | — |
| Explain a node | — | `GET /nodes/explain` | Explain mode | `explain_node` |
| Graph neighbors | — | `GET /graph/neighbors` | Graph workspace | `get_graph_neighbors` |
| Browse filesystem | — | `GET /fs/list` | Add-a-local-directory | — |

## Visualization

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Full graph | — | `GET /graph` | Cytoscape graph workspace | — |
| Graph slice (around a node) | — | `GET /graph/slice` | Drill-into sub-network | — |
| Export node-link JSON | — | `GET /graph/export/node-link-json` | — | — |
| Export Cytoscape JSON | — | `GET /graph/export/cytoscape-json` | — | — |
| Obsidian projection | — | `POST /obsidian/export` | — | `export_obsidian_notes` |

## Federation (Synapse region)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Inspect published contract | — | `GET /contract` | — | `get_contract` |
| Publish contract | (automatic on sync when `synapse.publish: true`) | — | — | — |
| Push event to router | (automatic webhook to `<router_url>/v1/synapse/events`) | — | — | — |

Routing, fan-out, merge, and global cross-region search live on the **router**
(subjective-retrieval), not on the region. See
[Attach to a Synapse fleet](../how-to/attach-to-synapse.md).

## Server & MCP lifecycle

| Capability | CLI |
|---|---|
| Start HTTP API + MCP | `syncsage start` |
| Serve (container entrypoint) | `syncsage serve` |
| Standalone MCP server | `syncsage mcp --transport stdio\|streamable-http\|sse` |
| Backup state | `syncsage backup <out>` |
| Restore state | `syncsage restore <in> [--force]` |

See [HTTP API](http-api.md) for the full route list and
[MCP tools & resources](../mcp_tools.md) for the full tool/resource list.
