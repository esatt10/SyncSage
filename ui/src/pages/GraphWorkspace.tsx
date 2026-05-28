import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphLink, GraphNode } from "../api/types";
import { GraphCanvas } from "../graph/GraphCanvas";
import { NodeInspector } from "../graph/NodeInspector";
import { Breadcrumbs } from "../graph/Breadcrumbs";
import { ALL_EDGE_TYPES, EDGE_COLORS, NODE_COLORS } from "../graph/graphStyles";
import { Explainable } from "../explain/Explainable";

export function GraphWorkspace() {
  const graphQuery = useQuery({ queryKey: ["graph"], queryFn: api.graph });
  const [extra, setExtra] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({
    nodes: [],
    links: [],
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusIds, setFocusIds] = useState<string[]>([]);
  const [expandDepth, setExpandDepth] = useState(1);
  const [enabledEdges, setEnabledEdges] = useState<Set<string>>(new Set(ALL_EDGE_TYPES));
  const [trail, setTrail] = useState<{ id: string; label: string }[]>([]);
  const [query, setQuery] = useState("");

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

  const visibleLinks = useMemo(
    () => merged.links.filter((l) => !l.type || enabledEdges.has(l.type)),
    [merged.links, enabledEdges],
  );

  const nodeById = useMemo(() => new Map(merged.nodes.map((n) => [n.id, n])), [merged.nodes]);

  const expand = useCallback(
    async (nodeId: string) => {
      const edgeTypes = enabledEdges.size === ALL_EDGE_TYPES.length ? undefined : [...enabledEdges];
      const slice = await api.graphSlice(nodeId, expandDepth, edgeTypes);
      setExtra((prev) => ({
        nodes: [...prev.nodes, ...slice.nodes],
        links: [...prev.links, ...slice.links],
      }));
      setFocusIds([nodeId, ...slice.nodes.map((n) => n.id)]);
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

  const searchQuery = useQuery({
    queryKey: ["search", query],
    queryFn: () => api.search(query, "hybrid", 10),
    enabled: query.trim().length > 2,
  });

  const locateResult = (relativePath: string) => {
    const match = merged.nodes.find((n) => n.relative_path === relativePath);
    if (match) {
      select(match.id);
      setFocusIds([match.id]);
    }
  };

  return (
    <div className="workspace">
      <aside className="legend-panel">
        <Explainable id="search.box" className="legend-block">
          <h3>Search</h3>
          <input
            className="text-input"
            placeholder="Hybrid search…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="search-results">
            {searchQuery.data?.results?.slice(0, 8).map((r, i) => (
              <button key={`${r.relative_path}-${i}`} className="search-hit" onClick={() => locateResult(r.relative_path)}>
                {r.relative_path}
              </button>
            ))}
          </div>
        </Explainable>

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
            <div key={type} className="legend-row">
              <span className="legend-dot" style={{ background: color }} />
              {type}
            </div>
          ))}
        </div>
      </aside>

      <section className="canvas-area">
        <div className="canvas-toolbar">
          <Breadcrumbs trail={trail} onJump={(i) => select(trail[i].id)} />
          <span className="muted small">
            {merged.nodes.length} nodes · {visibleLinks.length} edges
          </span>
        </div>
        <Explainable id="graph.canvas" className="canvas-host">
          {graphQuery.isLoading ? (
            <div className="centered muted">Loading graph…</div>
          ) : merged.nodes.length === 0 ? (
            <div className="centered muted">
              No graph yet. Add a source and sync it to populate the knowledge graph.
            </div>
          ) : (
            <GraphCanvas
              nodes={merged.nodes}
              links={visibleLinks}
              selectedId={selectedId}
              focusIds={focusIds}
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
