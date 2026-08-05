// Shapes returned by the pheasant HTTP API. Graph node/edge payloads are
// intentionally open (Record) because enrichment attaches type-specific fields.

export interface GraphNode {
  id: string;
  type?: string;
  label?: string;
  relative_path?: string;
  source_id?: string;
  hash?: string;
  summary?: string;
  provenance?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GraphLink {
  source: string;
  target: string;
  key?: number;
  type?: string;
  confidence?: number;
  [key: string]: unknown;
}

export interface NodeLinkGraph {
  nodes: GraphNode[];
  links: GraphLink[];
  total_nodes?: number;
  total_links?: number;
  /** Nodes matching the active type/source filter, before the node limit. */
  matched_nodes?: number;
  filtered?: boolean;
  truncated?: boolean;
}

export interface GraphSlice {
  node_id: string;
  depth: number;
  nodes: GraphNode[];
  links: GraphLink[];
  /** Hop distance from the slice's center, nearest wins. Center is 0. */
  depths?: Record<string, number>;
}

export interface NeighborEntry {
  node_id: string;
  depth: number;
  edge_types: string[];
  path: string[];
  node: GraphNode;
}

export interface NeighborsResponse {
  node_id: string;
  depth: number;
  neighbors: NeighborEntry[];
}

export interface ExplainResponse {
  node_id: string;
  type?: string;
  label?: string;
  explanation: string;
  provenance?: Record<string, unknown>;
  node?: GraphNode;
}

export interface SourceRecord {
  id: string;
  name: string;
  type: string;
  path: string;
  enabled: number | boolean;
  config_json?: string;
  last_status?: string;
  last_indexed_at?: string;
  checkpoint?: Record<string, unknown> | null;
  /** A background sync (registered/triggered with `wait: false`) is running now. */
  syncing?: boolean;
  /** Error from the most recent *background* sync, cleared by the next one. Independent of `last_status`. */
  sync_error?: string | null;
  [key: string]: unknown;
}

export interface SourceTypeInfo {
  id: string;
  label: string;
  description: string;
  /** "required" — the connector reads this path; "unused" — schema ceremony. */
  path_role: "required" | "unused";
  builtin: boolean;
}

export interface SourceTypeCatalog {
  types: SourceTypeInfo[];
  /** What to send as `path` for a type whose path_role is "unused". */
  placeholder_path: string;
}

export interface SourceWritePayload {
  name?: string;
  type?: string;
  path?: string;
  description?: string;
  enabled?: boolean;
  max_depth?: number | null;
  include?: string[];
  exclude?: string[];
  repo?: Record<string, unknown>;
  chunking?: Record<string, unknown>;
  sync?: Record<string, unknown>;
  connector?: Record<string, unknown>;
  urls?: string[];
  sync_now?: boolean;
  sync_mode?: string;
  /** false = register/sync without blocking the response; poll SourceRecord.syncing instead. Default true. */
  wait?: boolean;
}

export interface FsEntry {
  name: string;
  path: string;
  is_dir: boolean;
  // Root entries only: false when a configured allowlist root isn't mounted.
  mounted?: boolean;
}

export interface FsListing {
  path: string | null;
  parent: string | null;
  roots: string[];
  entries: FsEntry[];
}

export interface ConfigResponse {
  path: string;
  effective: Record<string, any>;
  raw_yaml: string | null;
  profiles: string[];
}

export type SearchMode = "hybrid" | "text" | "graph" | "vector";

export interface SearchResultItem {
  node_id?: string;
  kind?: "node" | "relationship" | "chunk";
  type?: string;
  title?: string;
  label?: string;
  relative_path?: string;
  source_id?: string;
  summary?: string;
  score?: number;
  match?: string;
  edge_type?: string;
  source?: string;
  target?: string;
  source_label?: string;
  target_label?: string;
  [key: string]: unknown;
}

export interface SearchResponse {
  results: SearchResultItem[];
  mode?: SearchMode;
  counts?: { text?: number; graph?: number; vector?: number; returned?: number };
  [key: string]: unknown;
}

export interface SyncResult {
  source_id: string;
  indexed_artifacts: number;
  skipped_artifacts: number;
  graph_nodes: number;
  graph_edges: number;
  status: string;
  error?: string;
}

/** GET /overview — everything the shell needs to decide what to render. */
export interface Overview {
  knowledge_base: string;
  name: string;
  description?: string;
  version: string;
  sources: SourceRecord[];
  source_count: number;
  indexed_artifacts: number;
  chunk_count: number;
  node_counts: Record<string, number>;
  total_nodes: number;
  total_links: number;
  /** False when there is nothing but the knowledge-base/source scaffolding. */
  has_content: boolean;
  config_path: string;
}

export type AssistantProviderId = "anthropic" | "openai" | "gemini";

export interface AssistantProvider {
  id: AssistantProviderId;
  label: string;
  default_model: string;
  api_key_env: string;
  key_hint: string;
  env_key_present: boolean;
}

export interface AssistantSession {
  provider: string;
  model: string | null;
  expires_at: string;
}

export interface AssistantStatus {
  enabled: boolean;
  providers: AssistantProvider[];
  configured_provider: string | null;
  configured_model: string | null;
  allow_session_keys: boolean;
  session: AssistantSession | null;
  /** False means answers are extractive rather than model-synthesized. */
  ready: boolean;
  /** Where the resolvable credential came from, or null when there is none. */
  credential_source: "session" | "environment" | null;
}

export interface Citation {
  index: number;
  node_id?: string;
  chunk_id?: string;
  title: string;
  relative_path?: string;
  source_id?: string;
  type?: string;
  score?: number;
  snippet: string;
  used: boolean;
}

export interface GraphFact {
  subject: string;
  subject_id: string;
  predicate: string;
  /** Object-first phrasing, e.g. "mentioned in". */
  predicate_passive?: string;
  edge_type: string;
  object: string;
  object_id: string;
  object_type: string;
  confidence?: number;
}

/** One recorded step of an agent workflow, for the "what did it do" trace. */
export interface WorkflowStep {
  name: string;
  detail: string;
  passages: number;
}

export interface WorkflowInfo {
  name: string;
  label: string;
  description: string;
  builtin: boolean;
}

export interface WorkflowCatalog {
  workflows: WorkflowInfo[];
  configured: string;
  /** What would actually run right now, after resolving "auto". */
  active: string;
  agent_extra_installed: boolean;
  options: Record<string, unknown>;
  option_defaults: Record<string, Record<string, unknown>>;
}

export interface EmbeddingsProvider {
  id: string;
  label: string;
  needs_key: boolean;
  description: string;
}

/** A vector backend, with whether its optional dependency is installed here. */
export interface VectorStoreProvider {
  id: string;
  label: string;
  available: boolean;
  hint: string | null;
}

export interface EmbeddingsStatus {
  enabled: boolean;
  /** Config says on AND a usable indexer was actually built. */
  active: boolean;
  provider: string;
  model: string;
  base_url: string;
  api_key_env: string;
  api_key_present: boolean;
  dimensions: number;
  batch_size: number;
  store_provider: string;
  store_path: string;
  vector_count: number;
  chunk_count: number;
  /** Fraction of indexed passages that have a vector. */
  coverage: number;
  dimensions_on_disk: number | null;
  store_error: string | null;
  providers: EmbeddingsProvider[];
  store_providers: VectorStoreProvider[];
  wrote_config?: boolean;
  vectors_invalidated?: boolean;
  reindex?: { embedded_chunks: number; artifacts_scanned: number; vector_count: number };
  embedded_chunks?: number;
  artifacts_scanned?: number;
}

export interface ChatAnswer {
  question: string;
  answer: string;
  mode: "llm" | "extractive";
  provider: string | null;
  model: string | null;
  credential_source: "session" | "environment" | null;
  error: string | null;
  citations: Citation[];
  facts: GraphFact[];
  focus_node_ids: string[];
  search_mode: string;
  counts: Record<string, number>;
  /** Which workflow produced this answer. */
  workflow?: string;
  /** The agent's trace; empty for the single-pass workflow. */
  steps?: WorkflowStep[];
}

export interface McpToolSummary {
  name: string;
  description: string;
}

export interface McpInfo {
  enabled: boolean;
  transports: Record<string, boolean>;
  streamable_http_url: string | null;
  stdio_command: string[];
  config_path: string;
  tools: McpToolSummary[];
  client_configs: Record<string, string>;
}

export interface QuickAddResponse {
  status: string;
  sources: { name: string; type: string; path: string; description?: string }[];
  sync_results: SyncResult[];
  /** Names of sources whose first sync was handed off to a background thread (wait: false). */
  syncing: string[];
}
