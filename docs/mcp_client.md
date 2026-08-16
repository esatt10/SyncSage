# MCP Client Setup

VS Code is the primary pheasant MCP client. The shared setup keeps host-specific paths out of source control.

## Files

| File | Commit? | Purpose |
|---|---:|---|
| `pheasant.example.yaml` | Yes | Shared pheasant config pattern. |
| `examples/vscode/mcp.json` | Yes | Reusable VS Code MCP template. |
| `pheasant.yaml` | No | User-specific runtime config. |
| `.pheasant/compose.env` | No | Generated Docker Compose env file. |
| `.vscode/mcp.json` | No | User-specific MCP client config. |

## Docker Service

```bash
cp pheasant.example.yaml pheasant.yaml
pheasant compose-env pheasant.yaml --output .pheasant/compose.env
docker compose --env-file .pheasant/compose.env up -d
curl http://localhost:8765/ready
```

Edit `deployment.compose.workspace_path` in `pheasant.yaml` so it points at the host folder containing repositories, notes, and documents to index. Source paths should still use container paths such as `/workspace/repository`.

## VS Code

Create the local MCP config:

```bash
mkdir -p .vscode
cp examples/vscode/mcp.json .vscode/mcp.json
```

Or generate it:

```bash
pheasant client-config vscode --output .vscode/mcp.json
```

The default config starts this command when VS Code starts the MCP server:

```bash
docker exec -i pheasant python -m pheasant mcp --config /config/pheasant.yaml --transport stdio
```

Then in VS Code:

1. Run `MCP: List Servers`.
2. Start `pheasant`.
3. Open Chat in Agent mode.
4. Use tools such as `list_knowledge_bases`, `sync_all`, and `search_context`.

If you change the compose container name, regenerate the config with:

```bash
pheasant client-config vscode --container-name <name> --output .vscode/mcp.json
```
