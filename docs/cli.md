# CLI

The bootstrap installs SyncSage into `.venv` with the MCP extra:

```bash
python scripts/bootstrap.py
```

Activate the virtual environment before running CLI commands directly:

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

After activation, `syncsage --help` should show the available commands. Without
activation, run `.venv/bin/syncsage` on macOS/Linux or
`.venv\Scripts\syncsage.exe` on Windows.

## Setup Commands

Render Docker Compose variables from YAML:

```bash
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
```

Generate VS Code MCP config for the running Compose container:

```bash
syncsage client-config vscode --output .vscode/mcp.json
```

Generate a one-off Docker-run MCP config instead of attaching to Compose:

```bash
syncsage client-config vscode --mode docker-run --output .vscode/mcp.json
```

Validate config shape without requiring container paths to exist on the host:

```bash
syncsage validate syncsage.yaml --no-require-paths
```

## Container Operator Commands

Start the API container:

```bash
docker compose --env-file .syncsage/compose.env up -d
```

Inspect logs:

```bash
docker compose --env-file .syncsage/compose.env logs -f syncsage
```

Run a full sync inside the container:

```bash
docker exec syncsage python -m syncsage sync --config /config/syncsage.yaml --all --mode full
```

Repair by rebuilding all enabled sources:

```bash
docker exec syncsage python -m syncsage repair --config /config/syncsage.yaml
```

Run the MCP stdio server manually for debugging:

```bash
docker exec -i syncsage python -m syncsage mcp --config /config/syncsage.yaml --transport stdio
```

## HTTP Commands

Check health and readiness:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

List configured sources:

```bash
curl http://localhost:8765/sources
```

Trigger an incremental sync:

```bash
curl -X POST http://localhost:8765/sync \
  -H "Content-Type: application/json" \
  -d "{\"mode\":\"incremental\"}"
```

Search indexed context:

```bash
curl -X POST http://localhost:8765/search \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"sync engine\",\"mode\":\"hybrid\",\"max_results\":5}"
```

Export Obsidian notes:

```bash
curl -X POST http://localhost:8765/obsidian/export
```

Generated notes appear under the host vault path from
`deployment.compose.vault_path`, usually `./vault/SyncSage/Index.md`.
