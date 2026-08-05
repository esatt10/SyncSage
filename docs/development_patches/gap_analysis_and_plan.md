# Gap Analysis and Execution Plan

## Intended product goal (north star)

pheasant should support:

1. Reliable sync for **nearly any directory source**, including local filesystems and cloud-backed mounts/connectors.
2. **Enriched knowledge graph formation** that captures semantic and structural relationships (not just file/chunk containment).
3. High-signal **Obsidian output** with practical navigation for human workflows.
4. Full **MCP-accessible navigation + read access** to existing context, plus **runtime registration of new context** beyond initial YAML.
5. Configuration ergonomics that preserve **one-line startup**, while exposing the **full configurable surface area** when needed.

---

## Current capability snapshot

### 1) Sync coverage

What exists now:
- Source types enumerated in config include `repository`, `markdown_folder`, `obsidian_vault`, `document_folder`, `web_collection`, and `single_file`.
- Incremental sync is hash-based (`sha256`) and skips unchanged files.
- Basic file watcher/git/scheduler settings are present in schema/docs.

Key gaps:
- `web_collection` and generalized cloud source semantics are declared in schema but do not have first-class connector execution paths in the sync engine.
- No connector abstraction for SaaS/API-backed sources (Google Drive, Notion, S3, SharePoint, etc.).
- No explicit per-source cursor/checkpoint model beyond local manifest file hash snapshots.
- Limited sync modes behavior: mode enum includes `validate_only` and `repair`, but source processing currently follows the same core path with no deep mode-specific orchestration.

### 2) Graph enrichment

What exists now:
- Graph nodes for knowledge base, sources, artifacts, and chunks.
- Edges mainly: `contains`, `indexes`, `has_chunk`.

Key gaps:
- No cross-artifact semantic links (references/citations/imports/calls/links/concept similarity).
- No typed entity extraction (symbols, concepts, people, systems, APIs).
- No graph freshness/version lineage edges for temporal reasoning.
- Neighbor traversal API currently behaves as depth-1 despite `depth` argument.

### 3) Obsidian output quality

What exists now:
- Export pipeline with configurable note/canvas toggles.

Key gaps:
- Navigation quality depends on richer graph edges that do not yet exist.
- No configurable note templates per source type/persona/workflow profile.
- Limited bidirectional linking strategy from concept/entity-level relationships.
- No export diff/preview mode for safe vault update workflows.

### 4) MCP context accessibility and runtime registration

What exists now:
- MCP tools include list/register/sync/search/repo-map/graph-neighbors/file-summary.
- Runtime `register_source` exists and records persistence metadata indicating YAML update is required.

Key gaps:
- Runtime registration is not fully lifecycle-managed (limited source provenance/state history and policies for permanence).
- Missing MCP-first resource navigation for richer browsing primitives (faceted source listing, cursor pagination, lineage/history views).
- No explicit MCP tools for promoting runtime-registered context into durable config (or generating patch snippets).
- Limited authorization/policy depth around runtime registration scope per transport/client identity.

### 5) Config ergonomics (one-line startup + full surface)

What exists now:
- YAML-driven config with broad documented sections.
- Compose env helper command and example config.

Key gaps:
- One-line startup exists operationally, but not as a first-class profile system with discoverable overrides.
- Large option surface is documented, but no layered config strategy (`baseline + profile + overrides`) for progressive complexity.
- No `pheasant init`/`pheasant doctor` UX for guided setup and validation with minimal friction.

---

## Recommended implementation plan

## Phase 1 — Solidify source sync architecture (highest priority)

### Deliverables
1. **Connector abstraction layer**
   - Introduce `SourceConnector` interface with capabilities:
     - `list_items()`
     - `read_item()`
     - `get_checkpoint()` / `set_checkpoint()`
     - `resolve_identity()` (stable IDs across renames/moves)
   - Implement `FilesystemConnector` first and migrate current logic behind it.

2. **Cloud-ready connector contract**
   - Add experimental connectors (stub/minimal): `S3Connector`, `WebCollectionConnector` (HTTP crawl snapshot), and generic `APIConnector` pattern.
   - Keep these off by default behind feature flags.

3. **True sync-mode semantics**
   - `incremental`: checkpoint/hash guided update.
   - `full`: rebuild artifacts/chunks/graph for source.
   - `validate_only`: verify accessibility, schema, permissions, and connector health; no writes.
   - `repair`: rebuild missing/invalid state artifacts only.

4. **Checkpoint model enhancements**
   - Persist per-source connector checkpoint/cursor plus high-watermark metadata in state DB.

### Acceptance criteria
- At least one non-filesystem connector path executes end-to-end in tests.
- `validate_only` performs no index writes.
- Source checkpoint state can be inspected via MCP/API status endpoints.

## Phase 2 — Graph enrichment and retrieval quality

### Deliverables
1. **Richer node/edge ontology**
   - Add node types: `symbol`, `entity`, `concept`, `external_reference`.
   - Add edge types: `references`, `imports`, `calls`, `similar_to`, `derived_from`, `mentions`.

2. **Pluggable enrichment passes**
   - Language-aware code pass (imports/symbol defs).
   - Markdown/doc pass (links/headings/citations).
   - Lightweight semantic similarity pass for cross-file relationships.

3. **Depth-aware graph traversal**
   - Implement true BFS/DFS traversal honoring `depth` and optional edge filters.

### Acceptance criteria
- `get_graph_neighbors(depth>1)` returns multi-hop traversal.
- Search relevance improves on cross-file tasks in benchmark fixtures.

## Phase 3 — Obsidian output as a navigable product surface

### Deliverables
1. **Template profiles**
   - `engineering`, `research`, `project-ops` note template packs.

2. **Graph-driven note linking**
   - Backlinks and related-sections sourced from enriched edges.

3. **Safe export workflows**
   - `preview` mode and export diff summary before write.

### Acceptance criteria
- Export preview shows planned note updates count + changed files.
- Generated vault supports jump navigation from source -> concept -> artifact -> chunk.

## Phase 4 — MCP-native context operations

### Deliverables
1. **Context lifecycle MCP tools**
   - `register_source` (existing, upgraded metadata)
   - `list_sources` with filters/status/pagination
   - `remove_source` / `disable_source`
   - `promote_runtime_source_to_config` (returns YAML patch or writes via policy)

2. **Context navigation resources**
   - Add paginated resources for source manifests, sync history, and graph slices.

3. **Policy + provenance**
   - Runtime registration audit trail (who/when/transport/client).

### Acceptance criteria
- New source can be registered at runtime, synced, queried, and promoted to config with deterministic output.

## Phase 5 — Configuration UX: one-line startup + full discoverability

### Recommended approach
Use a **layered configuration model**:

`Base defaults` + `Profile` + `User YAML` + `CLI/env overrides`

- **Base defaults**: safe local defaults (already mostly present).
- **Profile** (`quickstart`, `dev`, `team`, `cloud-hybrid`): prepackaged opinion sets.
- **User YAML**: full explicit surface for advanced customization.
- **Overrides**: highest-precedence one-off changes for automation.

### Concrete UX additions
1. `pheasant start --profile quickstart` (one-line run path).
2. `pheasant init --profile <name>` to generate commented YAML with only relevant sections expanded.
3. `pheasant config show --effective` to display resolved config after layering.
4. `pheasant doctor` to validate mounts, paths, connector auth, and MCP transport readiness.

### Why this is the best fit
- Preserves one-line startup for most users.
- Keeps full config surface available without forcing complexity at first run.
- Enables reproducibility by making effective config introspectable.

---

## Suggested execution order and milestones

1. **M1 (2–3 weeks):** Connector abstraction + sync-mode correctness + checkpoint state.
2. **M2 (2–4 weeks):** Graph enrichment core + depth traversal.
3. **M3 (1–2 weeks):** MCP lifecycle tools + runtime-to-config promotion.
4. **M4 (1–2 weeks):** Obsidian template profiles + preview diff mode.
5. **M5 (1 week):** Config UX commands and profile layering.

Parallelization guidance:
- Run M2 graph ontology design in parallel with late M1 connector implementation.
- Run M4 Obsidian template work in parallel with M3 MCP lifecycle APIs once enriched edges are available.

---

## Risks and mitigations

- **Risk:** Connector scope creep (too many providers too fast).  
  **Mitigation:** land stable connector interface + 1–2 connectors first.

- **Risk:** Graph enrichment increases index/storage costs.  
  **Mitigation:** make enrichment passes feature-flagged and source-type scoped.

- **Risk:** Runtime registration could bypass governance.  
  **Mitigation:** enforce policy checks and immutable audit logging for MCP registration actions.

- **Risk:** Config layering creates user confusion.  
  **Mitigation:** provide `config show --effective` and profile documentation with examples.

---

## Definition of done for this planning cycle

- A tracked implementation backlog exists for M1–M5 with explicit acceptance tests.
- Sync-mode semantics are test-verified.
- At least one non-local connector is production-shaped (even if marked beta).
- MCP runtime context lifecycle is complete from registration through durable config promotion.
- One-line startup and full config discoverability both work without conflict.
