# SyncSage Web UI

A light React + Vite front end for SyncSage. It is a **separate workload**: it
builds to a static bundle and talks to the SyncSage container purely over its
HTTP API, so the indexing container is unchanged (see
[`docs/ui_recommendation.md`](../docs/ui_recommendation.md)).

## Features

- **Graph workspace** — the knowledge graph rendered with Cytoscape. Drill from
  `knowledge_base → source → file → chunk/symbol/concept`, expand a node's
  sub-network (depth-bounded), filter by edge type (the "edge lens"), and pivot
  across relationships. Adding + syncing a source animates new nodes in.
- **Search** — query across nodes, relationships and their attributes. A **mode**
  control tunes retrieval (`hybrid` / `text` / `graph`) and a **results** control
  sets the hit count. Each hit shows its node type and relevance score; clicking
  one focuses the node, pulling it in from the bounded preview (and re-enabling a
  filtered node type) if it is not already on the canvas.
- **Sources** — list/sync/disable/remove, **add a source by opening a local
  directory** (allowlist-scoped browser), and **promote** runtime sources to
  YAML.
- **Config** — edit every config section via a form, drop to **raw YAML**, and
  **preview a diff** before validating and writing.
- **Explain mode** — a toggle that outlines explainable components and shows
  hover tooltips, plus a **"How it works"** Markdown panel with the numerical
  behavior in LaTeX (KaTeX).

## Develop

```bash
cd ui
npm install
# Point at a running SyncSage (defaults to http://localhost:8765):
SYNCSAGE_API_BASE=http://localhost:8765 npm run dev
```

In dev, API calls go to `/api/*` and Vite proxies them to `SYNCSAGE_API_BASE`.
Override the API base at build/runtime with `VITE_SYNCSAGE_API_BASE`.
The production UI requests a bounded initial graph preview by default
(`VITE_SYNCSAGE_GRAPH_NODE_LIMIT=1200`,
`VITE_SYNCSAGE_GRAPH_LINK_LIMIT=3600`) so large indexes do not block the browser;
node expansion still fetches focused graph slices.

## Build

```bash
npm run build      # outputs ui/dist
npm run preview    # serve the production build locally
```

## Deploy

- **Sidecar (recommended):** serve `ui/dist` from any static web server / nginx
  image next to SyncSage; set `VITE_SYNCSAGE_API_BASE` to the SyncSage URL.
- **Served by SyncSage (optional):** point SyncSage at the bundle with
  `SYNCSAGE_UI_DIST=/path/to/ui/dist`; it is mounted only when
  `server.ui.enabled` is true and the directory exists.

## Backend routes used

`/graph`, `/graph/slice`, `/graph/neighbors`, `/nodes/explain`, `/files/summary`,
`/sources` (GET/POST), `/sources/{name}/disable|promote` , `DELETE /sources/{name}`,
`/sync/{name}`, `/fs/list`, `/search`, `/config` (GET/PUT), `/health`.
