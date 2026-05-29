// Shapes returned by the SyncSage HTTP API. Graph node/edge payloads are
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
  truncated?: boolean;
}

export interface GraphSlice {
  node_id: string;
  depth: number;
  nodes: GraphNode[];
  links: GraphLink[];
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
  last_status?: string;
  last_indexed_at?: string;
  checkpoint?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface FsEntry {
  name: string;
  path: string;
  is_dir: boolean;
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

export interface SearchResultItem {
  relative_path: string;
  source_id?: string;
  summary?: string;
  score?: number;
  [key: string]: unknown;
}

export interface SearchResponse {
  results: SearchResultItem[];
  [key: string]: unknown;
}

export interface SyncResult {
  source_id: string;
  indexed_artifacts: number;
  skipped_artifacts: number;
  graph_nodes: number;
  graph_edges: number;
  status: string;
}
