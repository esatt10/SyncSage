import type {
  MemoryConsolidateResponse,
  MemoryListResponse,
  MemoryWriteResponse,
  AssistantStatus,
  ChatAnswer,
  ConfigResponse,
  EmbeddingsStatus,
  ExplainResponse,
  FsListing,
  GraphSlice,
  McpInfo,
  NeighborsResponse,
  NodeLinkGraph,
  Overview,
  QuickAddResponse,
  SearchMode,
  SearchResponse,
  SourceRecord,
  SourceTypeCatalog,
  SourceWritePayload,
  SyncResult,
  TaxonomyResponse,
  WorkflowCatalog,
} from "./types";
import type {
  ConfigSection,
  ConfigSectionResult,
  GraphDiagnostics,
  GraphPath,
  HostPathReport,
  JobRecord,
  KnowledgeBaseInfo,
  KnowledgeBaseUpdate,
  RetrievalResponse,
  RetrievalSettings,
  UploadResponse,
} from "./types";

// In dev, requests go to `/api/*` which Vite proxies to the pheasant container.
// When the bundle is served by pheasant itself, the API is same-origin (root).
const API_BASE =
  import.meta.env.VITE_PHEASANT_API_BASE ?? (import.meta.env.DEV ? "/api" : "");

function numericEnv(value: unknown, fallback: number): number {
  const parsed = Number(value ?? fallback);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

const GRAPH_NODE_LIMIT = numericEnv(import.meta.env.VITE_PHEASANT_GRAPH_NODE_LIMIT, 1200);
const GRAPH_LINK_LIMIT = numericEnv(import.meta.env.VITE_PHEASANT_GRAPH_LINK_LIMIT, 3600);

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

function qs(
  params: Record<string, string | number | boolean | string[] | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export interface GraphQueryOptions {
  excludeTypes?: string[];
  types?: string[];
  source?: string;
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  overview: () => request<Overview>("/overview"),

  // Graph
  graph: (options: GraphQueryOptions = {}) =>
    request<NodeLinkGraph>(
      `/graph${qs({
        limit: GRAPH_NODE_LIMIT,
        link_limit: GRAPH_LINK_LIMIT,
        exclude_types: options.excludeTypes?.length ? options.excludeTypes.join(",") : undefined,
        types: options.types?.length ? options.types.join(",") : undefined,
        source: options.source,
      })}`,
    ),
  /**
   * The sub-graph within `depth` hops of a node. `limit` caps how many
   * neighbours come back (BFS order, so nearest first) — the canvas asks for
   * far more than the API default, which exists for one-hop lookups.
   */
  graphSlice: (
    nodeId: string,
    depth = 1,
    options: {
      limit?: number;
      edgeTypes?: string[];
      excludeEdgeTypes?: string[];
      excludeTypes?: string[];
    } = {},
  ) =>
    request<GraphSlice>(
      `/graph/slice${qs({
        node_id: nodeId,
        depth,
        limit: options.limit,
        edge_types: options.edgeTypes?.join(","),
        exclude_edge_types: options.excludeEdgeTypes?.join(","),
        exclude_types: options.excludeTypes?.length
          ? options.excludeTypes.join(",")
          : undefined,
      })}`,
    ),
  graphNeighbors: (nodeId: string, depth = 1, edgeTypes?: string[]) =>
    request<NeighborsResponse>(
      `/graph/neighbors${qs({ node_id: nodeId, depth, edge_types: edgeTypes?.join(",") })}`,
    ),
  explain: (nodeId: string) =>
    request<ExplainResponse>(`/nodes/explain${qs({ node_id: nodeId })}`),
  fileSummary: (path: string, sourceName?: string) =>
    request<Record<string, unknown>>(`/files/summary${qs({ path, source_name: sourceName })}`),
  nodeContent: (nodeId: string) =>
    request<{ node_id: string; content: string | null }>(`/nodes/content${qs({ node_id: nodeId })}`),

  /**
   * The extracted outline of each document in a source with taxonomy on.
   * Read from the emitted `heading` nodes, so it reflects what was indexed
   * rather than re-parsing the file.
   */
  taxonomy: (options: { source?: string; path?: string } = {}) =>
    request<TaxonomyResponse>(`/taxonomy${qs({ source: options.source, path: options.path })}`),

  // Sources
  sources: () => request<SourceRecord[]>("/sources"),
  /** Every type this deployment accepts — built-ins plus installed plugins. */
  sourceTypes: () => request<SourceTypeCatalog>("/sources/types"),
  /** One-field source creation: a path, URL, glob or connector name. */
  quickAdd: (body: {
    target: string;
    name?: string;
    split?: boolean;
    sync_now?: boolean;
    wait?: boolean;
  }) =>
    request<QuickAddResponse>("/sources/quick-add", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  registerSource: (body: SourceWritePayload & { name: string; path: string }) =>
    request<{ status: string; source: Record<string, unknown> }>("/sources", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateSource: (name: string, body: SourceWritePayload) =>
    request<{ status: string; source: Record<string, unknown> }>(
      `/sources/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  disableSource: (name: string) =>
    request<{ status: string }>(`/sources/${encodeURIComponent(name)}/disable`, { method: "POST" }),
  removeSource: (name: string) =>
    request<{ status: string }>(`/sources/${encodeURIComponent(name)}`, { method: "DELETE" }),
  promoteSource: (name: string, write = false) =>
    request<{ status: string; yaml_patch: string; wrote_config: boolean }>(
      `/sources/${encodeURIComponent(name)}/promote`,
      { method: "POST", body: JSON.stringify({ write }) },
    ),
  /** `wait: false` (the UI's default for interactive use) returns as soon as the
   * sync is handed to a background thread; poll `sources()` for `syncing`/`sync_error`. */
  syncSource: (name: string, mode = "incremental", wait = false) =>
    request<SyncResult | { status: string; source_id: string }>(
      `/sync/${encodeURIComponent(name)}`,
      { method: "POST", body: JSON.stringify({ mode, wait }) },
    ),
  syncAll: (mode = "incremental", wait = false) =>
    request<{ results: SyncResult[] } | { status: string; sources: string[] }>("/sync", {
      method: "POST",
      body: JSON.stringify({ mode, wait }),
    }),

  // Filesystem (allowlist-scoped)
  fsList: (path?: string) => request<FsListing>(`/fs/list${qs({ path })}`),

  // Search
  search: (query: string, mode: SearchMode = "hybrid", maxResults = 10) =>
    request<SearchResponse>("/search", {
      method: "POST",
      body: JSON.stringify({ query, mode, max_results: maxResults }),
    }),

  // Assistant (grounded chat)
  assistantStatus: (sessionId?: string | null) =>
    request<AssistantStatus>(`/assistant/status${qs({ session_id: sessionId ?? undefined })}`),
  /** Hand a key to the server for this session only — it is never persisted. */
  assistantKey: (body: {
    provider: string;
    api_key: string;
    model?: string;
    base_url?: string;
  }) =>
    request<{ session_id: string; session: { provider: string; expires_at: string } }>(
      "/assistant/key",
      { method: "POST", body: JSON.stringify(body) },
    ),
  assistantRevoke: (sessionId: string) =>
    request<{ revoked: boolean }>(`/assistant/key${qs({ session_id: sessionId })}`, {
      method: "DELETE",
    }),
  /** Every question-answering workflow this deployment can run. */
  workflows: () => request<WorkflowCatalog>("/assistant/workflows"),
  chat: (body: {
    question: string;
    session_id?: string | null;
    mode?: string;
    max_results?: number;
    source_name?: string | null;
    workflow?: string | null;
    options?: Record<string, unknown>;
  }) =>
    request<ChatAnswer>("/assistant/chat", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * The same answer, with each workflow step delivered as it completes.
   *
   * The agent loop can run for a while over a large index, so `onStep` fires
   * per stage (plan, retrieve, grade, …) and the promise resolves with the
   * finished answer. Falls back to nothing special on the client side: if the
   * stream breaks, the error propagates like any other failed request.
   */
  chatStream: async (
    body: {
      question: string;
      session_id?: string | null;
      mode?: string;
      max_results?: number;
      source_name?: string | null;
      workflow?: string | null;
      options?: Record<string, unknown>;
      memory?: string | null;
    },
    onStep: (step: { name: string; detail: string; passages: number }) => void,
    signal?: AbortSignal,
  ): Promise<ChatAnswer> => {
    const response = await fetch(`${API_BASE}/assistant/chat/stream`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`${response.status} ${response.statusText}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let answer: ChatAnswer | null = null;

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line; a frame may arrive split
      // across chunks, so only complete ones are consumed.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const event = JSON.parse(line.slice(5).trim());
        if (event.type === "step") onStep(event);
        else if (event.type === "answer") answer = event.answer as ChatAnswer;
        else if (event.type === "error") throw new Error(event.error);
      }
    }
    if (!answer) throw new Error("the answer stream closed before an answer arrived");
    return answer;
  },

  // Semantic search (embeddings)
  embeddings: () => request<EmbeddingsStatus>("/search/embeddings"),
  updateEmbeddings: (body: {
    enabled?: boolean;
    provider?: string;
    model?: string;
    base_url?: string;
    api_key_env?: string;
    /** `null` clears the override back to the model's own native size. */
    dimensions?: number | null;
    batch_size?: number;
    store_provider?: string;
    persist?: boolean;
    reindex?: boolean;
  }) =>
    request<EmbeddingsStatus>("/search/embeddings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** Embed already-indexed content without re-reading the sources. */
  rebuildVectors: (dropExisting = false) =>
    request<EmbeddingsStatus>(`/search/embeddings/reindex${qs({ drop_existing: dropExisting })}`, {
      method: "POST",
    }),

  // MCP
  mcpInfo: () => request<McpInfo>("/mcp/info"),

  // Config
  getConfig: () => request<ConfigResponse>("/config"),
  putConfig: (body: { config?: Record<string, unknown>; yaml_text?: string }) =>
    request<{ status: string; restart_required: boolean }>("/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // Uploads — documents dropped into the browser become a real source.
  // Not `request()`: that sets a JSON content-type, and a multipart body
  // needs the browser to set its own boundary header.
  uploadDocuments: async (files: File[], sourceName = "uploads"): Promise<UploadResponse> => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("source_name", sourceName);
    form.append("sync_now", "true");
    form.append("wait", "false");
    const response = await fetch(`${API_BASE}/sources/upload`, { method: "POST", body: form });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* no JSON body */
      }
      throw new Error(detail);
    }
    return (await response.json()) as UploadResponse;
  },

  // Host paths — "can pheasant see this, and if not, exactly how do I fix it".
  hostPath: (path: string) => request<HostPathReport>(`/fs/host-path${qs({ path })}`),

  // Jobs
  jobs: (activeOnly = false) =>
    request<{ jobs: JobRecord[]; active_count: number }>(`/jobs${qs({ active: activeOnly })}`),
  job: (jobId: string) => request<JobRecord>(`/jobs/${encodeURIComponent(jobId)}`),
  clearJob: (jobId: string) =>
    request<{ cleared: number }>(`/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),
  clearJobs: () => request<{ cleared: number }>("/jobs", { method: "DELETE" }),

  // Retrieval tuning
  retrieval: () => request<RetrievalResponse>("/assistant/retrieval"),
  updateRetrieval: (body: Partial<RetrievalSettings> & { persist?: boolean }) =>
    request<RetrievalResponse & { changed: string[]; wrote_config: boolean }>(
      "/assistant/retrieval",
      { method: "PUT", body: JSON.stringify(body) },
    ),

  // Live config sections + knowledge-base identity
  configSections: () => request<{ sections: ConfigSection[] }>("/config/sections"),
  patchConfigSection: (section: string, values: Record<string, unknown>, persist = true) =>
    request<ConfigSectionResult>(`/config/section/${encodeURIComponent(section)}`, {
      method: "PATCH",
      body: JSON.stringify({ values, persist }),
    }),
  knowledgeBase: () => request<KnowledgeBaseInfo>("/knowledge-base"),
  updateKnowledgeBase: (body: { name?: string; description?: string; persist?: boolean }) =>
    request<KnowledgeBaseUpdate>("/knowledge-base", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // Agent memory (Step 33.10). These endpoints have existed since 33.1;
  // nothing in the UI had ever called them.
  memory: (params: { scope?: string; current_only?: boolean } = {}) =>
    request<MemoryListResponse>(`/memory${qs(params)}`),
  memoryWrite: (body: {
    text: string;
    scope?: string;
    subject?: string | null;
    supersedes?: string | null;
    tags?: string[];
    kind?: string;
    principal?: string | null;
    sync?: boolean;
  }) => request<MemoryWriteResponse>("/memory", { method: "POST", body: JSON.stringify(body) }),
  memoryConsolidate: () =>
    request<MemoryConsolidateResponse>("/memory/consolidate", { method: "POST" }),
  // `type: memory` is deliberately absent from the generic source picker, so
  // this is the one way a person can turn memory on from the UI.
  memoryEnable: () =>
    request<{ status: string; source: SourceRecord }>("/memory/enable", {
      method: "POST",
      body: JSON.stringify({}),
    }),

  // Graph diagnostics + path finding (the full-screen workspace)
  graphDiagnostics: (top = 20) =>
    request<GraphDiagnostics>(`/graph/diagnostics${qs({ top })}`),
  graphPath: (source: string, target: string, maxDepth = 8) =>
    request<GraphPath>(`/graph/path${qs({ source, target, max_depth: maxDepth })}`),
};

export { API_BASE };
