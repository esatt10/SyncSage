import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphLink, GraphNode, SearchMode, SearchResultItem } from "../api/types";
import { GraphCanvas } from "../graph/GraphCanvas";
import { NodeInspector } from "../graph/NodeInspector";
import { Breadcrumbs } from "../graph/Breadcrumbs";
import {
  ALL_EDGE_TYPES,
  ALL_NODE_TYPES,
  EDGE_COLORS,
  NODE_COLORS,
  shapeForNodeType,
  type ShapeAlgorithm,
} from "../graph/graphStyles";
import { Explainable } from "../explain/Explainable";

export function GraphWorkspace() {
  const graphQuery = useQuery({ queryKey: ["graph"], queryFn: api.graph });
  const persisted = useMemo(readGraphWorkspaceState, []);
  const [extra, setExtra] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({
    nodes: persisted.extra?.nodes ?? [],
    links: persisted.extra?.links ?? [],
  });
  const [selectedId, setSelectedId] = useState<string | null>(persisted.selectedId ?? null);
  const [focusIds, setFocusIds] = useState<string[]>(persisted.focusIds ?? []);
  const [expandDepth, setExpandDepth] = useState(persisted.expandDepth ?? 1);
  const [enabledEdges, setEnabledEdges] = useState<Set<string>>(
    new Set(persisted.enabledEdges ?? ALL_EDGE_TYPES),
  );
  const [enabledNodeTypes, setEnabledNodeTypes] = useState<Set<string>>(
    new Set(persisted.enabledNodeTypes ?? ALL_NODE_TYPES),
  );
  const [layoutName, setLayoutName] = useState(persisted.layoutName ?? "auto");
  const [shapeAlgorithm, setShapeAlgorithm] = useState<ShapeAlgorithm>(
    persisted.shapeAlgorithm ?? "node_type",
  );
  const [spacing, setSpacing] = useState(persisted.spacing ?? 1);
  const debouncedSpacing = useDebouncedValue(spacing, 160);
  const [showTrail, setShowTrail] = useState(persisted.showTrail ?? true);
  const [trail, setTrail] = useState<{ id: string; label: string }[]>(persisted.trail ?? []);
  const [query, setQuery] = useState(persisted.query ?? "");
  const [searchMode, setSearchMode] = useState<SearchMode>(persisted.searchMode ?? "hybrid");
  const [searchLimit, setSearchLimit] = useState(persisted.searchLimit ?? 10);
  const [expandingNode, setExpandingNode] = useState<string | null>(null);
  const [locating, setLocating] = useState(false);

  const base = graphQuery.data ?? { nodes: [], links: [] };

  // Merge the base graph with any expanded sub-network slices, deduplicating.
  const merged = useMemo(() => {
    const nodeMap = new Map<string, GraphNode>();
    [...base.nodes, ...extra.nodes].forEach((n) => n.id && nodeMap.set(n.id, n));
    const linkKey = (l: GraphLink) => `${l.source}|${l.type}|${l.key ?? 0}|${l.target}`;
    const linkMap = new Map<string, GraphLink>();
    [...base.links, ...extra.links].forEach((l) => linkMap.set(linkKey(l), l));
    return { nodes: [...nodeMap.values()], links: [...linkMap.values()] };
  }, [base, extra]);
  const totalNodes = graphQuery.data?.total_nodes ?? merged.nodes.length;
  const totalLinks = graphQuery.data?.total_links ?? merged.links.length;

  const visibleNodes = useMemo(
    () =>
      merged.nodes.filter(
        (node) => !node.type || !ALL_NODE_TYPES.includes(node.type) || enabledNodeTypes.has(node.type),
      ),
    [merged.nodes, enabledNodeTypes],
  );
  const visibleNodeIds = useMemo(() => new Set(visibleNodes.map((node) => node.id)), [visibleNodes]);
  const visibleLinks = useMemo(
    () =>
      merged.links.filter(
        (link) =>
          (!link.type || enabledEdges.has(link.type)) &&
          visibleNodeIds.has(link.source) &&
          visibleNodeIds.has(link.target),
      ),
    [merged.links, enabledEdges, visibleNodeIds],
  );

  const nodeById = useMemo(() => new Map(merged.nodes.map((n) => [n.id, n])), [merged.nodes]);

  useEffect(() => {
    writeGraphWorkspaceState({
      extra,
      selectedId,
      focusIds,
      expandDepth,
      enabledEdges: [...enabledEdges],
      enabledNodeTypes: [...enabledNodeTypes],
      layoutName,
      shapeAlgorithm,
      spacing,
      showTrail,
      trail,
      query,
      searchMode,
      searchLimit,
    });
  }, [
    extra,
    selectedId,
    focusIds,
    expandDepth,
    enabledEdges,
    enabledNodeTypes,
    layoutName,
    shapeAlgorithm,
    spacing,
    showTrail,
    trail,
    query,
    searchMode,
    searchLimit,
  ]);

  const expand = useCallback(
    async (nodeId: string) => {
      setExpandingNode(nodeId);
      try {
        const edgeTypes = enabledEdges.size === ALL_EDGE_TYPES.length ? undefined : [...enabledEdges];
        const slice = await api.graphSlice(nodeId, expandDepth, edgeTypes);
        setExtra((prev) => ({
          nodes: [...prev.nodes, ...slice.nodes],
          links: [...prev.links, ...slice.links],
        }));
        setFocusIds([nodeId, ...slice.nodes.map((n) => n.id)]);
      } finally {
        setExpandingNode(null);
      }
    },
    [enabledEdges, expandDepth],
  );

  const select = useCallback(
    (nodeId: string) => {
      setSelectedId(nodeId);
      const label = nodeById.get(nodeId)?.label ?? nodeId;
      setTrail((prev) => {
        const existing = prev.findIndex((c) => c.id === nodeId);
        if (existing >= 0) return prev.slice(0, existing + 1);
        return [...prev, { id: nodeId, label }];
      });
    },
    [nodeById],
  );

  const toggleEdge = (type: string) => {
    setEnabledEdges((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const toggleNodeType = (type: string) => {
    setEnabledNodeTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const searchQuery = useQuery({
    queryKey: ["search", query, searchMode, searchLimit],
    queryFn: () => api.search(query, searchMode, searchLimit),
    enabled: query.trim().length > 1,
  });

  // Re-enable a node type that is currently filtered out so a located node is
  // actually visible on the canvas instead of silently hidden.
  const ensureTypeVisible = useCallback((type?: string) => {
    if (!type || !ALL_NODE_TYPES.includes(type)) return;
    setEnabledNodeTypes((prev) => (prev.has(type) ? prev : new Set(prev).add(type)));
  }, []);

  // Navigate to a search hit by its node id. When the node is part of a
  // truncated graph preview it may not be loaded yet, so we pull in its
  // neighborhood slice before focusing it; relative_path is a fallback for
  // legacy text hits that don't carry a node id.
  const locateNode = useCallback(
    async (result: SearchResultItem) => {
      const id = result.node_id;
      if (id && nodeById.has(id)) {
        ensureTypeVisible(nodeById.get(id)?.type);
        select(id);
        setFocusIds([id]);
        return;
      }
      if (result.relative_path) {
        const match = merged.nodes.find((n) => n.relative_path === result.relative_path);
        if (match) {
          ensureTypeVisible(match.type);
          select(match.id);
          setFocusIds([match.id]);
          return;
        }
      }
      if (!id) return;
      setLocating(true);
      try {
        const slice = await api.graphSlice(id, 1);
        if (slice.nodes.length > 0) {
          setExtra((prev) => ({
            nodes: [...prev.nodes, ...slice.nodes],
            links: [...prev.links, ...slice.links],
          }));
          slice.nodes.forEach((n) => ensureTypeVisible(n.type));
          select(id);
          setFocusIds([id, ...slice.nodes.map((n) => n.id)]);
        }
      } finally {
        setLocating(false);
      }
    },
    [nodeById, merged.nodes, select, ensureTypeVisible],
  );

  return (
    <div className="workspace">
      <aside className="legend-panel">
        <Explainable id="search.box" className="legend-block">
          <h3>Search</h3>
          <input
            className="text-input"
            placeholder="Search nodes, edges, attributes…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="search-controls">
            <label className="field field--compact">
              <span>Mode</span>
              <select
                className="text-input text-input--small"
                value={searchMode}
                onChange={(e) => setSearchMode(e.target.value as SearchMode)}
              >
                <option value="hybrid">hybrid</option>
                <option value="text">text</option>
                <option value="graph">graph</option>
                <option value="vector" title="Semantic search; needs search.embeddings enabled">
                  vector
                </option>
              </select>
            </label>
            <label className="field field--compact">
              <span>Results</span>
              <input
                className="text-input text-input--small"
                type="number"
                min={1}
                max={100}
                value={searchLimit}
                onChange={(e) =>
                  setSearchLimit(Math.max(1, Math.min(100, Number(e.target.value) || 1)))
                }
              />
            </label>
          </div>
          <div className="search-results">
            {searchQuery.isFetching ? <span className="muted small">Searching…</span> : null}
            {locating ? <span className="muted small">Loading node…</span> : null}
            {!searchQuery.isFetching &&
            query.trim().length > 1 &&
            (searchQuery.data?.results?.length ?? 0) === 0 ? (
              <span className="muted small">No matches.</span>
            ) : null}
            {searchQuery.data?.results?.map((r, i) => (
              <button
                key={`${r.node_id ?? r.relative_path ?? "hit"}-${i}`}
                className="search-hit"
                onClick={() => locateNode(r)}
                title={searchHitTitle(r)}
              >
                <span className="search-hit__main">{searchHitLabel(r)}</span>
                <span className="search-hit__meta">
                  {r.type ? <span className="search-hit__type">{r.type}</span> : null}
                  {typeof r.score === "number" ? (
                    <span className="search-hit__score">{r.score.toFixed(2)}</span>
                  ) : null}
                </span>
              </button>
            ))}
          </div>
        </Explainable>

        <div className="legend-block">
          <h3>Graph layout</h3>
          <label className="field field--compact">
            <span>Algorithm</span>
            <select className="text-input text-input--small" value={layoutName} onChange={(event) => setLayoutName(event.target.value)}>
              <option value="auto">auto</option>
              <option value="cose">force</option>
              <option value="grid">grid</option>
              <option value="circle">circle</option>
              <option value="concentric">concentric</option>
              <option value="breadthfirst">breadthfirst</option>
            </select>
          </label>
          <label className="field field--compact">
            <span>Shape algorithm</span>
            <select
              className="text-input text-input--small"
              value={shapeAlgorithm}
              onChange={(event) => setShapeAlgorithm(event.target.value as ShapeAlgorithm)}
            >
              <option value="node_type">node type</option>
              <option value="degree">relationship degree</option>
              <option value="uniform">uniform</option>
            </select>
          </label>
          <label className="field field--compact">
            <span>Spacing</span>
            <input
              type="range"
              min="0.6"
              max="1.8"
              step="0.1"
              value={spacing}
              onChange={(event) => setSpacing(Number(event.target.value))}
            />
          </label>
        </div>

        <Explainable id="graph.legend" className="legend-block">
          <h3>Edge lens</h3>
          {ALL_EDGE_TYPES.map((type) => (
            <label key={type} className="legend-row">
              <input type="checkbox" checked={enabledEdges.has(type)} onChange={() => toggleEdge(type)} />
              <span className="legend-swatch" style={{ background: EDGE_COLORS[type] }} />
              {type}
            </label>
          ))}
        </Explainable>

        <div className="legend-block">
          <h3>Node types</h3>
          {Object.entries(NODE_COLORS).map(([type, color]) => (
            <label key={type} className="legend-row">
              <input type="checkbox" checked={enabledNodeTypes.has(type)} onChange={() => toggleNodeType(type)} />
              <span
                className={`legend-shape legend-shape--${legendShape(shapeForNodeType(type))}`}
                style={{ background: color }}
                title={`${type} shape: ${shapeForNodeType(type)}`}
              />
              {type}
            </label>
          ))}
        </div>
      </aside>

      <section className="canvas-area">
        <div className="canvas-toolbar">
          <div className="canvas-toolbar__left">
            {showTrail ? <Breadcrumbs trail={trail} onJump={(i) => select(trail[i].id)} /> : null}
            <label className="checkbox checkbox--inline">
              <input
                type="checkbox"
                checked={showTrail}
                onChange={(event) => setShowTrail(event.target.checked)}
              />
              Traversal path
            </label>
            {trail.length > 0 ? (
              <button className="btn btn--small" onClick={() => setTrail([])}>
                clear path
              </button>
            ) : null}
          </div>
          <span className="muted small">
            {visibleNodes.length}
            {graphQuery.data?.truncated ? ` of ${totalNodes}` : ""} nodes · {visibleLinks.length}
            {graphQuery.data?.truncated ? ` of ${totalLinks}` : ""} edges
          </span>
        </div>
        {graphQuery.data?.truncated ? (
          <div className="graph-limit-banner">
            Showing a bounded graph preview. Search or expand nodes to inspect focused neighborhoods.
          </div>
        ) : null}
        {expandingNode ? (
          <div className="graph-limit-banner">Loading neighborhood for {nodeById.get(expandingNode)?.label ?? expandingNode}...</div>
        ) : null}
        <Explainable id="graph.canvas" className="canvas-host">
          {graphQuery.isLoading ? (
            <div className="centered muted">Loading graph…</div>
          ) : visibleNodes.length === 0 ? (
            <div className="centered muted">
              No graph yet. Add a source and sync it to populate the knowledge graph.
            </div>
          ) : (
            <GraphCanvas
              nodes={visibleNodes}
              links={visibleLinks}
              selectedId={selectedId}
              focusIds={focusIds}
              layoutName={layoutName}
              spacing={debouncedSpacing}
              shapeAlgorithm={shapeAlgorithm}
              onSelect={select}
            />
          )}
        </Explainable>
      </section>

      <aside className="inspector-panel">
        <NodeInspector
          selectedId={selectedId}
          nodes={merged.nodes}
          links={merged.links}
          expandDepth={expandDepth}
          onDepthChange={setExpandDepth}
          onExpand={expand}
          onPivot={select}
        />
      </aside>
    </div>
  );
}

function legendShape(shape: string): string {
  return shape.replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
}

function searchHitLabel(result: SearchResultItem): string {
  if (result.kind === "relationship") {
    return `${result.source_label ?? result.source ?? ""} → ${result.target_label ?? result.target ?? ""}`;
  }
  return String(result.title ?? result.label ?? result.relative_path ?? result.node_id ?? "");
}

function searchHitTitle(result: SearchResultItem): string {
  const parts = [searchHitLabel(result)];
  if (result.kind === "relationship" && result.edge_type) parts.push(`(${result.edge_type})`);
  if (result.match) parts.push(`matched: ${result.match}`);
  return parts.join(" ");
}

interface PersistedGraphWorkspace {
  extra?: { nodes: GraphNode[]; links: GraphLink[] };
  selectedId?: string | null;
  focusIds?: string[];
  expandDepth?: number;
  enabledEdges?: string[];
  enabledNodeTypes?: string[];
  layoutName?: string;
  shapeAlgorithm?: ShapeAlgorithm;
  spacing?: number;
  showTrail?: boolean;
  trail?: { id: string; label: string }[];
  query?: string;
  searchMode?: SearchMode;
  searchLimit?: number;
}

const GRAPH_STATE_KEY = "syncsage.graph.workspace.v2";

function readGraphWorkspaceState(): PersistedGraphWorkspace {
  try {
    const raw = sessionStorage.getItem(GRAPH_STATE_KEY);
    return raw ? (JSON.parse(raw) as PersistedGraphWorkspace) : {};
  } catch {
    return {};
  }
}

function writeGraphWorkspaceState(state: PersistedGraphWorkspace): void {
  try {
    sessionStorage.setItem(GRAPH_STATE_KEY, JSON.stringify(state));
  } catch {
    /* session storage may be unavailable or full */
  }
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(handle);
  }, [value, delayMs]);
  return debounced;
}
