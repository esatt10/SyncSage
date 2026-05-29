import type {
  ConfigResponse,
  ExplainResponse,
  FsListing,
  GraphSlice,
  NeighborsResponse,
  NodeLinkGraph,
  SearchResponse,
  SourceRecord,
  SyncResult,
} from "./types";

// In dev, requests go to `/api/*` which Vite proxies to the SyncSage container.
// When the bundle is served by SyncSage itself, the API is same-origin (root).
const API_BASE =
  import.meta.env.VITE_SYNCSAGE_API_BASE ?? (import.meta.env.DEV ? "/api" : "");

function numericEnv(value: unknown, fallback: number): number {
  const parsed = Number(value ?? fallback);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

const GRAPH_NODE_LIMIT = numericEnv(import.meta.env.VITE_SYNCSAGE_GRAPH_NODE_LIMIT, 1200);
const GRAPH_LINK_LIMIT = numericEnv(import.meta.env.VITE_SYNCSAGE_GRAPH_LINK_LIMIT, 3600);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* response had no JSON body */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

function qs(params: Record<string, string | number | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  // Graph
  graph: () =>
    request<NodeLinkGraph>(
      `/graph${qs({ limit: GRAPH_NODE_LIMIT, link_limit: GRAPH_LINK_LIMIT })}`,
    ),
  graphSlice: (nodeId: string, depth = 1, edgeTypes?: string[]) =>
    request<GraphSlice>(
      `/graph/slice${qs({ node_id: nodeId, depth, edge_types: edgeTypes?.join(",") })}`,
    ),
  graphNeighbors: (nodeId: string, depth = 1, edgeTypes?: string[]) =>
    request<NeighborsResponse>(
      `/graph/neighbors${qs({ node_id: nodeId, depth, edge_types: edgeTypes?.join(",") })}`,
    ),
  explain: (nodeId: string) =>
    request<ExplainResponse>(`/nodes/explain${qs({ node_id: nodeId })}`),
  fileSummary: (path: string, sourceName?: string) =>
    request<Record<string, unknown>>(`/files/summary${qs({ path, source_name: sourceName })}`),

  // Sources
  sources: () => request<SourceRecord[]>("/sources"),
  registerSource: (body: {
    name: string;
    type: string;
    path: string;
    description?: string;
    include?: string[];
    exclude?: string[];
    sync_now?: boolean;
    sync_mode?: string;
  }) => request<{ status: string; source: Record<string, unknown> }>("/sources", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  disableSource: (name: string) =>
    request<{ status: string }>(`/sources/${encodeURIComponent(name)}/disable`, { method: "POST" }),
  removeSource: (name: string) =>
    request<{ status: string }>(`/sources/${encodeURIComponent(name)}`, { method: "DELETE" }),
  promoteSource: (name: string, write = false) =>
    request<{ status: string; yaml_patch: string; wrote_config: boolean }>(
      `/sources/${encodeURIComponent(name)}/promote`,
      { method: "POST", body: JSON.stringify({ write }) },
    ),
  syncSource: (name: string, mode = "incremental") =>
    request<SyncResult>(`/sync/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  syncAll: (mode = "incremental") =>
    request<{ results: SyncResult[] }>("/sync", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),

  // Filesystem (allowlist-scoped)
  fsList: (path?: string) => request<FsListing>(`/fs/list${qs({ path })}`),

  // Search
  search: (query: string, mode = "hybrid", maxResults = 10) =>
    request<SearchResponse>("/search", {
      method: "POST",
      body: JSON.stringify({ query, mode, max_results: maxResults }),
    }),

  // Config
  getConfig: () => request<ConfigResponse>("/config"),
  putConfig: (body: { config?: Record<string, unknown>; yaml_text?: string }) =>
    request<{ status: string; restart_required: boolean }>("/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};

export { API_BASE };
