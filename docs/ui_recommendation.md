# SyncSage Web UI — Design Recommendation

> Status: proposal / design recommendation. No application code is introduced by
> this document. It specifies a **light React front end** for SyncSage, grounded
> in the existing FastAPI surface (`src/syncsage/api/app.py`), the configuration
> schema (`src/syncsage/config/schema.py`), and the knowledge-graph model
> (`docs/graph_model.md`).

## 1. Goals and constraints

The UI exists to make SyncSage approachable without removing the power of the
YAML workflow. Concretely:

1. **Keep YAML as a first-class path.** The existing `syncsage.example.yaml` +
   profile + `--set` override model stays exactly as-is. The UI is an *additive*
   surface that reads and writes the same effective config; it never becomes the
   only way in. Operators who prefer files keep using them.
2. **Lower the setup cliff.** Initial YAML setup is the reported friction point.
   The UI replaces "edit YAML blind, restart, read logs" with a guided,
   validated, form-driven flow that shows the resolved config before it is
   applied.
3. **Network-first visualization.** The primary view is the knowledge graph, not
   a table. Sources, files, concepts, symbols, and their relationships render as
   a navigable network. Adding a source immediately shows up as new nodes.
4. **Open a local directory and add it.** A directory picker lets a user choose a
   filesystem path under an allowlisted root and register it as a source.
5. **Navigable abstractions.** Browsing should feel like traversing a knowledge
   graph: expand a node, pivot on an edge type, drill from a source into its
   files, concepts, and chunks, and back out — without losing context.
6. **Explain mode.** A toggle that, when on, surfaces a hover tooltip on every
   component describing what it does, backed by a full Markdown reference panel
   whose numerical behavior is written in LaTeX.
7. **All core config options are reachable in the UI.** Every field in the config
   schema is editable through the UI, organized to match the YAML sections.
8. **The SyncSage container is its own workload and does not change.** The UI is a
   separate workload (static bundle / sidecar), not new weight inside the
   indexing container. See §8.

## 2. Where the UI plugs into what already exists

SyncSage already ships almost everything a read/visualize UI needs. The table
maps each UI capability to an existing backend affordance, and flags the few
gaps that a future backend PR would need to close (this doc does not implement
them).

| UI capability | Backing endpoint / module | Status |
|---|---|---|
| Render the knowledge graph | `GET /graph/export/cytoscape-json` (`api/app.py:161`) | **Exists** — Cytoscape-shaped payload is already produced by `graph/exporter.py`. |
| Raw graph / node-link export | `GET /graph`, `GET /graph/export/node-link-json` | Exists |
| List sources + status + checkpoints | `GET /sources`, `GET /sync/status` | Exists |
| Trigger sync (all / one, moded) | `POST /sync`, `POST /sync/{source_id}` | Exists |
| Search content (hybrid/keyword/graph) | `POST /search`, `POST /relevant-files` | Exists |
| Obsidian export / preview | `POST /obsidian/export` | Exists |
| Health / readiness / liveness badges | `GET /health`, `GET /ready`, `GET /metrics` | Exists |
| Knowledge-base list | `GET /knowledge-bases` | Exists |
| Expand a node's neighbors (sub-network) | `SyncSageTools.get_graph_neighbors`, `get_graph_slice` | **MCP-only today** — needs a thin HTTP route to be UI-reachable. |
| Explain a node | `SyncSageTools.explain_node` | MCP-only today |
| File summary / repo map | `get_file_summary`, `get_repo_map` | MCP-only today |
| Register a source from a directory | `SyncSageTools.register_source` (+ `security.path_policy.resolve_under`) | MCP-only today |
| Browse the local filesystem to pick a directory | — | **Gap** — needs a sandboxed `GET /fs/list` route restricted to `allow_workspace_roots`. |
| Read/write effective config | `config/loader.py` (`effective_config_dict`, `load_layered_config`) | Exists as library calls; **needs** `GET/PUT /config` HTTP routes. |
| Audit / sync history timeline | `get_sync_history`, `state.list_source_audit_events` | MCP-only today |

**Design consequence:** the UI is mostly assembled from endpoints that already
exist. The work to make it fully functional is concentrated in a small,
well-bounded set of HTTP routes that re-expose already-implemented
`SyncSageTools` methods (graph traversal, register, explain, history) plus two
new ones (`/fs/list`, `/config`). Keeping that backend work minimal is exactly
why the read-only graph + search experience can ship first.

## 3. Recommended technology

Deliberately light. No SSR, no global state framework, no design-system bloat.

| Concern | Recommendation | Why |
|---|---|---|
| Build / dev server | **Vite + React 18 + TypeScript** | Fast, tiny config, produces a static bundle that any web server (or FastAPI `StaticFiles`) can serve. |
| Graph rendering | **Cytoscape.js** (`react-cytoscapejs`) | The backend already emits `cytoscape-json`. Zero impedance mismatch. Handles thousands of nodes, supports compound nodes, layouts (fcose/cose-bilkent), edge styling per type, and incremental add/remove for live updates. |
| Data fetching / cache | **TanStack Query** | Polling sync status, cache invalidation after a sync, optimistic graph updates. ~13 kB. |
| Forms / config | **react-hook-form + Zod** | The config schema is a typed tree; Zod mirrors it and gives per-field validation that matches `validate_source_paths` semantics. |
| Markdown + math | **react-markdown + remark-math + rehype-katex (KaTeX)** | Powers the Explain reference panel with LaTeX numerical formulas (§6). |
| Styling | **CSS modules or Tailwind** (team choice) | Keep it minimal; no heavy component kit required. |
| Routing | **React Router** (3 top-level routes) | Graph, Sources, Config. |

Bundle target: a single static `dist/` under ~400 kB gzipped (Cytoscape is the
largest dependency). No backend language change; the indexing container stays
Python-only.

## 4. Information architecture

Three top-level routes, with the graph as the home view.

```
/                -> Graph workspace (default)
/sources         -> Source manager + add-from-directory
/config          -> Configuration editor (all schema sections)
  global overlays: Explain toggle, Health badge, KB switcher, Command palette
```

### 4.1 Graph workspace (home)

The centerpiece — "navigating a knowledge graph" made literal.

- **Canvas:** Cytoscape view fed by `/graph/export/cytoscape-json`. Node color/
  shape encodes `type` (source / file / chunk / symbol / entity / concept /
  external_reference); edge style encodes `type` (`contains`, `mentions`,
  `imports`, `calls`, `similar_to`, `references`, …).
- **Abstraction levels (navigable abstractions).** Default render collapses to
  the **source layer** (one node per source + knowledge-base root). The user
  drills down progressively:
  `knowledge_base → source → file/document → chunk/symbol/concept`.
  Each expand fetches just that node's slice (see sub-network below) instead of
  loading the entire graph, which keeps the canvas legible and the payload small.
- **Sub-network investigation.** Selecting a node opens an **inspector panel**
  and, on "Expand," calls the graph-neighbors route (depth + optional
  `edge_types` filter, mirroring `get_graph_neighbors` / `get_graph_slice`). The
  returned nodes/links are merged into the canvas with a focus-and-fade
  treatment so the explored sub-network is highlighted and the rest dims. A
  breadcrumb trail records the traversal path (`get_graph_neighbors` already
  returns `path`), so users can step back up the abstraction ladder.
- **Edge-type lens.** A legend doubles as a filter: toggle `similar_to` to see
  semantic clusters, toggle `imports`/`calls` to see code dependency structure,
  toggle `mentions`/`references` to see concept linkage. This directly exposes
  the enrichment passes described in `docs/graph_model.md`.
- **Relationships & content.** The inspector has tabs:
  - *Overview* — node type, label, provenance (source id, relative path, content
    hash, indexed timestamp, branch/commit) from `explain_node`.
  - *Relationships* — incoming/outgoing edges grouped by type, each clickable to
    pivot the canvas focus to that neighbor.
  - *Content* — for file/chunk nodes, the chunk text / file summary
    (`get_file_summary`); for source nodes, the repo map (`get_repo_map`).
- **Live add → live render.** When a source is added (§4.2) and synced, the
  client invalidates the graph query and animates the new nodes/edges into the
  canvas (Cytoscape incremental `add()`), so "adding items shows on the network
  diagram" is immediate and visible rather than requiring a reload.

### 4.2 Source manager + add-from-directory

- A list of sources from `GET /sources` with status, type, enabled flag, last
  sync, and checkpoint info (the registry already returns `checkpoint` per
  source).
- **Open a local directory.** An "Add source" flow opens a directory browser
  backed by a sandboxed `GET /fs/list?path=...` route. The browser only ever
  shows paths under `security.allow_workspace_roots`; the route reuses
  `security/path_policy.resolve_under` so traversal outside allowlisted roots is
  rejected server-side (the UI never gets to request a path the policy forbids).
- After picking a directory, an inline form collects the rest of a `SourceConfig`
  (type, include/exclude globs with sensible defaults pre-filled from
  `DEFAULT_EXCLUDES`, chunking strategy, per-source sync triggers). Submitting
  calls a `register_source` HTTP route (re-exposing the existing tool), then
  offers an immediate **Sync now** (`POST /sync/{source_id}`), after which the
  graph view animates the new subgraph in.
- **Runtime vs. durable.** The UI mirrors the existing lifecycle: a freshly added
  source is runtime-registered (the tool already returns
  `config_update_required: true`). A **"Promote to YAML"** action surfaces the
  `promote_runtime_source_to_config` patch so the user can persist it — keeping
  the YAML workflow authoritative and honest about what is/isn't saved to disk.
- Per-source actions: **Sync** (mode selector: incremental / full /
  validate_only / repair), **Disable**, **Remove**, **View in graph**.

### 4.3 Configuration editor (all core options)

Every section of `SyncSageConfig` is editable, laid out to mirror the YAML so
the mental model transfers both directions:

| UI panel | Schema source | Notable fields |
|---|---|---|
| Instance | `SyncSageSettings` | name, description, environment, log_level, state/vault/workspace/exports paths |
| Server | `ServerSettings` / `McpSettings` / `ApiSettings` / `UiSettings` | host, port, MCP transports (stdio/streamable_http/sse), api.enabled, api.openapi, **ui.enabled, ui.graph_visualization** |
| Storage | `StorageSettings` | graph_format, snapshot interval, sqlite/graph/manifest paths, max_state_size_gb, compression, retention |
| Search | `SearchSettings` | default_mode, keyword engine, embeddings, vector_store, ranking boosts, max_results_default |
| Sync | `SyncSettings` (watcher/git/scheduler/idempotency/concurrency) | debounce_ms, batch_window_ms, git triggers, scheduler interval, concurrency caps |
| Obsidian | `ObsidianSettings` | enabled, write_mode, note_root, template_profile, note/canvas toggles, frontmatter, backlinks, tags |
| Security | (config security block) | allow_workspace_roots, read_only_sources, deny_path_traversal, default_exclude_secrets |
| Sources | `SourceConfig[]` | full per-source editor (see §4.2) |

Editor behavior:

- **Profile-aware.** A profile picker (`quickstart` / `dev` / `team` /
  `cloud-hybrid`) seeds the form using the same layering the CLI uses
  (`load_layered_config`). The form shows base + profile + user overrides as the
  effective values, matching `syncsage config show --effective`.
- **Diff before apply.** Saving shows a YAML diff (the rendered effective config
  vs. current file) — the UI equivalent of inspecting the resolved config — then
  writes via a `PUT /config` route. This is the single biggest cure for the
  "YAML was hard to set up" complaint: validation + preview before commit.
- **Validation.** Client-side Zod mirrors the schema; server-side validation
  reuses `validate_source_paths` so path/allowlist errors are reported the same
  way `syncsage doctor` reports them.
- **YAML escape hatch.** A "Raw YAML" tab shows the underlying file and lets
  power users edit text directly — the UI and YAML stay two views of one source
  of truth, satisfying "we don't have to take YAML away."

## 5. Explain mode (hover highlights)

A global toggle in the top bar. Two coordinated behaviors:

1. **Hover tooltips.** With Explain on, hovering any interactive component (a
   node type in the legend, an edge filter, a sync-mode button, a config field,
   a status badge) shows a concise tooltip describing what that component does
   and which backend behavior it maps to. Implementation: a single
   `<ExplainProvider>` context + a `data-explain="key"` attribute on components;
   the provider looks the key up in a shared registry and renders the tooltip
   only when Explain is active. This keeps the explanatory text in one auditable
   place and out of the component bodies.
2. **Component highlight.** While Explain is on, components with an explanation
   get a subtle outline so users can discover what is explainable, and the hovered
   component is emphasized while siblings dim — the same focus-and-fade language
   used in the graph canvas, for consistency.

The tooltip text and the reference panel (§6) draw from the **same content
source**, so there is one definition per concept, surfaced in two densities
(short tooltip ↔ full panel).

## 6. Markdown + LaTeX reference panel

A slide-over panel ("How SyncSage works") rendered with
`react-markdown + remark-math + rehype-katex`. It documents each component in
prose and expresses the **numerical** behavior in LaTeX. These formulas are
transcriptions of behavior already in the codebase, so the docs stay truthful.

Representative entries (the panel ships the full set):

**Chunking** (`ingestion/chunking.py`, `ChunkingSettings`). With chunk size
$C$ (`max_chars`) and overlap $O$ (`overlap_chars`), the stride and the chunk
count for a document of length $L$ characters are:

$$\text{stride} = C - O, \qquad N_{\text{chunks}} = \left\lceil \frac{L - O}{C - O} \right\rceil$$

so larger overlap $O$ increases redundancy and total chunk count for the same
$L$.

**Content idempotency** (`sync.idempotency`). A file is reprocessed only when its
identity tuple changes:

$$\text{reindex} \iff \big(s, m, H(\text{bytes})\big) \neq \big(s', m', H'\big), \quad H = \mathrm{SHA\text{-}256}$$

where $s$ is size, $m$ is mtime, and $H$ is the content hash — matching
`compare_size_mtime_hash` and `skip_unchanged_files`.

**Watcher debounce / batching** (`sync.watcher`). For a burst of file events with
inter-arrival gaps, a change is dispatched after the debounce window
$\tau_d$ (`debounce_ms`) of quiet, and events are coalesced within the batch
window $\tau_b$ (`batch_window_ms`):

$$\text{dispatch at } t = t_{\text{last event}} + \tau_d, \qquad \text{batch} = \{e_i : t_{e_i} \le t_0 + \tau_b\}$$

**Hybrid ranking** (`search`, `ranking.*`). The hybrid score blends keyword
relevance with graph-derived term expansion and the configured boosts:

$$\mathrm{score}(d) = \alpha\, \mathrm{kw}(d) + \beta \!\!\sum_{t \in \mathcal{G}(q)}\!\! \mathrm{w}(t,d) + \gamma\, b_{\text{path}}(d) + \delta\, b_{\text{recent}}(d) + \varepsilon\, b_{\text{neighbor}}(d)$$

where $\mathcal{G}(q)$ is the set of graph-expanded terms for query $q$, and the
boost terms correspond to `prefer_exact_path_matches`, `prefer_recent_commits`,
and `graph_neighbor_boost`.

**Concept similarity** (`graph/enrichment.py`, `similar_to` edges). Two artifacts
$a$ and $b$ are linked when the Jaccard overlap of their normalized concept sets
clears a threshold $\theta$:

$$J(a,b) = \frac{|\,\mathcal{C}_a \cap \mathcal{C}_b\,|}{|\,\mathcal{C}_a \cup \mathcal{C}_b\,|}, \qquad a \xrightarrow{\text{similar\_to}} b \iff J(a,b) \ge \theta$$

**Graph traversal depth** (`get_graph_neighbors`). Expansion is bounded by
$d \in [1, 10]$ (the code clamps `max(1, min(depth, 10))`); the visited frontier
at depth $d$ from node $v$ is the set of nodes reachable by $\le d$ matching
edges, optionally filtered to an allowed edge-type set $\mathcal{E}$.

Each formula sits next to a plain-language paragraph and a link that focuses the
relevant config field in the editor — closing the loop between "what it means"
and "where I change it."

## 7. Component breakdown (light)

```
src/
  app/                     # router, providers (Query, Explain, KB context)
  api/                     # typed fetch wrappers, one per endpoint group
  graph/
    GraphCanvas.tsx        # Cytoscape wrapper, layouts, incremental add/remove
    GraphLegend.tsx        # node/edge type legend = edge-type lens/filter
    NodeInspector.tsx      # Overview / Relationships / Content tabs
    Breadcrumbs.tsx        # traversal path (abstraction ladder)
  sources/
    SourceList.tsx
    AddSourceWizard.tsx    # directory picker + SourceConfig form
    DirectoryBrowser.tsx   # /fs/list, allowlist-scoped
    SyncControls.tsx       # mode selector, status polling
  config/
    ConfigEditor.tsx       # section panels mirroring schema
    SectionPanels/*        # Instance, Server, Storage, Search, Sync, Obsidian, Security
    ConfigDiff.tsx         # YAML diff before apply
    RawYamlTab.tsx         # escape hatch
  explain/
    ExplainProvider.tsx    # toggle + data-explain registry
    ExplainTooltip.tsx
    ReferencePanel.tsx     # react-markdown + KaTeX
    content/*.md           # shared explanation source (tooltip + panel)
  common/
    HealthBadge.tsx        # /health, /ready, /metrics
    KnowledgeBaseSwitcher.tsx
    CommandPalette.tsx     # quick jump to node/source/search
```

## 8. Deployment — the SyncSage container stays its own workload

This is a hard constraint: adding a UI must not change what the SyncSage
indexing container *is* or how it is operated. The recommendation keeps them
decoupled and offers two deployment shapes, both leaving the existing container
build (`Dockerfile`, `docker-compose.yml`) functionally unchanged.

**Build-time:** the React app builds to a static `dist/`. It talks to SyncSage
purely over the existing HTTP API. There is no Python/runtime coupling.

- **Option A — sidecar (recommended).** Ship the UI as a *separate* container
  (e.g. an nginx/static-file image serving `dist/`) in the same compose project /
  Kubernetes pod, alongside SyncSage. The SyncSage container is untouched; it
  remains a self-contained indexing workload with its own lifecycle, health
  check, and state volume. The UI container is independently
  scalable/removable, and in `team`/`prod` you can drop it entirely without
  touching SyncSage. This best honors "the SyncSage container should be
  considered its own workload."

  ```yaml
  # additive compose service; existing syncsage service is unchanged
  syncsage-ui:
    image: ghcr.io/esatt10/syncsage-ui:0.1.x
    ports: ["8080:80"]
    environment:
      SYNCSAGE_API_BASE: http://syncsage:8765
    depends_on: [syncsage]
  ```

- **Option B — optional static mount.** If a single-container deployment is
  preferred for `quickstart`, SyncSage can *optionally* serve the prebuilt
  `dist/` via FastAPI `StaticFiles`, gated behind the already-present
  `server.ui.enabled` toggle, and only when the bundle is present. This adds no
  new runtime dependency to the image (static files only) and stays off in
  `team`/`prod` where `ui.enabled` / `api.openapi` are disabled per the security
  guidance. It is strictly opt-in and does not alter default container behavior.

Either way: the indexing workload's responsibilities, image contents, and
operational contract do not change. The `server.ui.*` config flags that already
exist are the natural on/off switch.

## 9. Security posture for the UI

The UI inherits, and must not weaken, the existing controls:

- **Directory browsing and source registration are allowlist-bound.** `/fs/list`
  and the register route resolve every path through `path_policy.resolve_under`
  against `allow_workspace_roots`; the UI cannot reach paths the policy forbids,
  and `deny_path_traversal` still applies.
- **Read-only by default.** Mutating routes (register / sync / config write /
  obsidian export) should be explicitly enabled and, for `team`/`prod`, sit
  behind ingress auth — consistent with `docs/security.md`'s "bind local API/UI
  carefully" note. Read-only graph + search can be exposed first.
- **Untrusted indexed content.** The Content tab renders chunk/file text as
  data, never as executable instructions, preserving the prompt-injection
  posture: provenance is always shown, content is never auto-acted-upon.
- **No code execution.** The UI triggers only the existing retrieval/sync/export
  operations; it introduces no path that executes indexed repository code.

## 10. Suggested phasing

1. **Phase 1 — read-only graph + search (highest value, smallest backend work).**
   Graph workspace, node inspector, search, source list, health badge, Explain
   mode + reference panel. Uses only endpoints that already exist plus thin HTTP
   wrappers over `get_graph_neighbors` / `explain_node` / `get_file_summary`.
2. **Phase 2 — write paths.** Add-from-directory (`/fs/list` + register), sync
   controls, promote-to-YAML.
3. **Phase 3 — full config editor.** All schema sections, profile layering, diff
   preview, raw YAML tab, `GET/PUT /config`.

Each phase is independently shippable and leaves the SyncSage container as its
own unchanged workload.

## 11. Backend additions this design assumes (not implemented here)

For transparency, the only backend work this UI needs — all thin re-exposures of
existing, tested logic except the two marked NEW:

- `GET /graph/neighbors`, `GET /graph/slice` → wrap `SyncSageTools.get_graph_neighbors` / `get_graph_slice`.
- `GET /nodes/{id}/explain` → wrap `explain_node`.
- `GET /files/summary`, `GET /sources/{name}/repo-map` → wrap `get_file_summary` / `get_repo_map`.
- `POST /sources` (register), `POST /sources/{name}/promote` → wrap `register_source` / `promote_runtime_source_to_config`.
- `GET /sources/{name}/history` → wrap `get_sync_history`.
- `GET /fs/list` **(NEW)** → allowlist-scoped directory listing via `resolve_under`.
- `GET /config`, `PUT /config` **(NEW)** → effective-config read + validated write via `config/loader.py`.

These keep the indexing container Python-only and reuse the security and
validation primitives already present in the codebase.
