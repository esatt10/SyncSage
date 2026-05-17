# MCP Client Setup

VS Code is the primary SyncSage MCP client. The shared setup keeps host-specific paths out of source control.

## Files

| File | Commit? | Purpose |
|---|---:|---|
| `syncsage.example.yaml` | Yes | Shared SyncSage config pattern. |
| `examples/vscode/mcp.json` | Yes | Reusable VS Code MCP template. |
| `syncsage.yaml` | No | User-specific runtime config. |
| `.syncsage/compose.env` | No | Generated Docker Compose env file. |
| `.vscode/mcp.json` | No | User-specific MCP client config. |

## Docker Service

```bash
cp syncsage.example.yaml syncsage.yaml
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d
curl http://localhost:8765/ready
```

Edit `deployment.compose.workspace_path` in `syncsage.yaml` so it points at the host folder containing repositories, notes, and documents to index. Source paths should still use container paths such as `/workspace/repository`.

## VS Code

Create the local MCP config:

```bash
mkdir -p .vscode
cp examples/vscode/mcp.json .vscode/mcp.json
```

Or generate it:

```bash
syncsage client-config vscode --output .vscode/mcp.json
```

The default config starts this command when VS Code starts the MCP server:

```bash
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

Then in VS Code:

1. Run `MCP: List Servers`.
2. Start `syncsage`.
3. Open Chat in Agent mode.
4. Use tools such as `list_knowledge_bases`, `sync_all`, `search_context`, and `export_obsidian_notes`.

If you change the compose container name, regenerate the config with:

```bash
syncsage client-config vscode --container-name <name> --output .vscode/mcp.json
```

## Obsidian

Set `deployment.compose.vault_path` in `syncsage.yaml` to the host folder you want to open in Obsidian. SyncSage writes managed notes to `/vault/SyncSage`, which appears as `SyncSage/` in that host folder. Open the host folder in Obsidian and use `export_obsidian_notes` after indexing.
