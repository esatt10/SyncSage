# SyncSage Web UI

A React + Vite front end for SyncSage: a three-pane research workspace over
your indexed knowledge base. It is a **separate workload** — it builds to a
static bundle and talks to the SyncSage container purely over its HTTP API, so
the indexing container is unchanged (see
[`docs/ui_recommendation.md`](../docs/ui_recommendation.md)).

```
┌──────────┬──────────────────────────┬──────────────────────┐
│ Sources  │ Chat                     │ Graph / Facts / Node │
│  scope   │  question → grounded     │  cited nodes lit up  │
│  + add   │  answer with [1] chips   │  surfaced triples    │
└──────────┴──────────────────────────┴──────────────────────┘
```

## Features

- **Chat** — ask in prose; answers are grounded in your own index and cite the
  passages they came from. Clicking a `[1]` chip focuses that node on the graph
  and opens it in the inspector. Works with **Anthropic**, **OpenAI** or
  **Gemini**, and works *without* any of them (extractive answers). Full
  behavior: [Ask your knowledge base](../docs/how-to/chat-and-ui.md).
- **Facts** — subject–predicate–object triples read straight off the graph one
  hop from each cited passage, collected round-robin so one hub document cannot
  crowd out the rest. Both endpoints of a fact are clickable.
- **Graph** — Cytoscape canvas with a small shape vocabulary and a legend that
  doubles as the type filter. `concept` and `chunk` are hidden by default (they
  routinely make up most of a real index). Expand a node's sub-network, pivot
  across relationships, inspect identity/links/content.
- **Sources** — one field takes a path, URL, glob or connector name and
  SyncSage detects the rest; selecting a source scopes both chat and graph to
  it. An advanced form still exposes every option.
- **Onboarding** — when nothing is indexed you get an actionable empty state
  (add a source, or the one-line command), not an empty canvas.
- **Connect agent** — MCP transports, a ready-to-paste `.mcp.json`, and the
  tool list an attached coding agent gets.
- **Settings** — edit every config section via a form, drop to raw YAML, and
  preview a diff before validating and writing.

## Design notes

- **Light by default.** This is a reading tool; dark is opt-in via the theme
  toggle and persists in `localStorage`. Both themes come from the CSS custom
  properties at the top of `styles.css` — change a token, not a rule.
- **Small shape vocabulary.** Four shapes, not twelve: rounded rectangles for
  containers, rectangles for documents, circles for ideas, diamonds for code
  symbols. Colour carries the finer distinction; size grows with connectivity.
- **Nothing secret is stored here.** A chat API key the user pastes goes to the
  server, which holds it in memory behind an opaque token; the browser keeps
  only that token, in `sessionStorage`.

## Develop

```bash
cd ui
npm install
npm run dev        # http://localhost:5173, proxying /api -> :8765
```

Point the proxy elsewhere with `SYNCSAGE_API_BASE`:

```bash
SYNCSAGE_API_BASE=http://localhost:9000 npm run dev
```

## Build

```bash
npm run typecheck
npm run build      # outputs ui/dist
npm run preview    # serve the production build locally
```

| Build variable | Default | Purpose |
|---|---|---|
| `VITE_SYNCSAGE_API_BASE` | `/api` in dev, same-origin in prod | Where the API lives. |
| `VITE_SYNCSAGE_GRAPH_NODE_LIMIT` | `1200` | Node budget per graph request. |
| `VITE_SYNCSAGE_GRAPH_LINK_LIMIT` | `3600` | Link budget per graph request. |

The bounded preview keeps large indexes from blocking the browser; node
expansion still fetches focused graph slices on demand.

## Deploy

- **Sidecar (default):** `docker compose up` — or `syncsage host <target>` —
  builds this image and serves it behind nginx, proxying `/api/*` to the
  SyncSage container.
- **Served by SyncSage:** build to `dist/` and the API mounts it automatically,
  or point `SYNCSAGE_UI_DIST` at a bundle elsewhere. Mounted only when
  `server.ui.enabled` is true and the directory exists.

## Backend routes used

`/overview`, `/graph`, `/graph/slice`, `/graph/neighbors`, `/nodes/explain`,
`/nodes/content`, `/files/summary`, `/sources` (GET/POST),
`/sources/quick-add`, `/sources/{name}/disable|promote`,
`DELETE /sources/{name}`, `/sync/{name}`, `/fs/list`, `/search`,
`/assistant/status|key|chat`, `/mcp/info`, `/config` (GET/PUT), `/health`.
