// Single source of short explanations, keyed by `data-explain` id. Tooltips and
// the reference panel both draw from SyncSage's documented behavior so the UI
// text stays truthful to the backend.

export const explainRegistry: Record<string, string> = {
  "graph.canvas":
    "The knowledge graph. Nodes are sources, files, chunks, symbols, concepts and entities; edges are relationships like contains, mentions, imports, calls and similar_to.",
  "graph.legend":
    "Legend and edge-type lens. Toggle an edge type to filter the graph — e.g. show only `similar_to` to see semantic clusters, or `imports`/`calls` for code structure.",
  "graph.expand":
    "Expand this node's sub-network. Fetches neighbors up to the chosen depth (bounded 1–10) so you drill in without loading the entire graph.",
  "graph.depth":
    "Traversal depth for expansion. SyncSage clamps this to the range 1–10 when walking the graph.",
  "graph.breadcrumbs":
    "Your traversal path through abstraction levels: knowledge_base → source → file → chunk/symbol/concept. Click an ancestor to step back up.",
  "inspector.overview":
    "Node identity and provenance: type, label, source id, relative path, content hash, branch and commit when available.",
  "inspector.relationships":
    "Incoming and outgoing edges grouped by type. Click a neighbor to pivot the graph focus to it.",
  "inspector.content":
    "Indexed content for this node: chunk text or a file summary for artifacts, drawn from the SQLite store.",
  "sources.add":
    "Register a new source by opening a local directory. Browsing is restricted server-side to allowlisted roots (workspace, vault, exports).",
  "sources.directory":
    "Allowlist-scoped directory browser. You can only reach paths under SyncSage's configured roots; path traversal is rejected by the server.",
  "sources.syncMode":
    "Sync mode. incremental skips unchanged files via checkpoints/hashes; full rebuilds everything; validate_only checks health without writing; repair rebuilds only missing/invalid state.",
  "sources.promote":
    "A runtime-registered source lives only in state until promoted. This writes it into your YAML so the change is durable.",
  "config.profile":
    "Built-in profile that seeds defaults (quickstart / dev / team / cloud-hybrid). The effective config is base + profile + your edits.",
  "config.diff":
    "Preview the YAML difference before applying. This is the UI equivalent of `syncsage config show --effective`.",
  "config.rawYaml":
    "Edit the underlying YAML directly. The form and the YAML are two views of one source of truth — SyncSage never takes YAML away.",
  "config.apply":
    "Validate and write the config to disk. Path/allowlist checks mirror `syncsage doctor`. Some changes require a SyncSage restart to take effect.",
  "explain.toggle":
    "Explain mode. While on, hover any highlighted component for a description of what it does and which backend behavior it maps to.",
  "health.badge":
    "Live service health from /health and /ready, including the active knowledge base.",
  "search.box":
    "Hybrid retrieval over indexed content: SQLite full-text combined with graph-derived term expansion and ranking boosts.",
};
