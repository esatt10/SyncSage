# Attach your knowledge base to a coding agent

Give Claude Code, Cursor, or any MCP client direct access to your indexed
knowledge — search, graph traversal, and provenance-tagged context — in
about five minutes (Product Framework Step 30.5).

## 1. Index something

If you haven't yet, the one-command path:

```bash
pip install pheasant-kb
pheasant up ~/notes --no-serve     # detect → generate config → index
```

`up` auto-detects an Obsidian vault (`.obsidian/`), a git repository
(`.git/`), or a plain document folder, and anchors all state under
`./.pheasant/` next to the generated `pheasant.yaml`.

## 2. Generate the client config

One command per agent — both emit the shared `mcpServers` JSON shape:

```bash
# Claude Code: project-scoped .mcp.json in your repo root
pheasant client-config claude-code -c pheasant.yaml -o .mcp.json

# Cursor: .cursor/mcp.json
pheasant client-config cursor -c pheasant.yaml -o .cursor/mcp.json
```

The default `--mode local` runs the pip-installed `pheasant` binary over
stdio — no docker required. For containerized regions use
`--mode docker-exec` (attach to a running `pheasant` container) or
`--mode docker-run` (start one per session); these reuse the same argument
vectors as `pheasant client-config vscode`.

## 3. Use it

Restart the agent (or approve the new MCP server when prompted). The agent
now sees the pheasant tools — `search_context`, `get_relevant_files`, graph
neighbors, sync triggers — over your indexed sources. Ask it things like
*"find the notes where I compared retention policies"* and it will cite
chunks with file-level provenance.

## Fleet-wide access (many knowledge bases)

One region = one KB. To let an agent search **across a fleet** of regions,
attach the Synapse router's MCP host instead — `pflock-mcp` from the
pheasant-flock package exposes `synapse_search` / `synapse_route` /
`synapse_list_kbs` over the whole fleet. See the router-side guide:
[MCP how-to](https://esatt10.github.io/pheasant-flock/how-to/mcp/).
