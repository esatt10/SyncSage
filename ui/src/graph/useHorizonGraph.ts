import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphLink, GraphNode } from "../api/types";

/**
 * The canvas never draws the whole graph by default.
 *
 * A real index is tens of thousands of nodes; drawn at once it is a hairball
 * that answers no question. So the view is a *horizon*: everything within N
 * hops of one center node, N defaulting to 3 and adjustable by the user. After
 * a question the horizon is drawn around the nodes the answer actually
 * surfaced, so the canvas shows the evidence rather than the universe.
 */

export type HorizonMode = "center" | "answer" | "all";

export interface HorizonGraph {
  nodes: GraphNode[];
  links: GraphLink[];
  /** Hop distance from the nearest center; missing for the unbounded view. */
  depths: Record<string, number>;
  mode: HorizonMode;
  /** Nodes matching the filters before the horizon/limit was applied. */
  matchedNodes?: number;
}

export interface HorizonOptions {
  centerId: string | null;
  depth: number;
  showAll: boolean;
  surfacedIds: string[];
  hiddenTypes: string[];
  sourceFilter: string | null;
  enabled?: boolean;
}

/** Neighbours per slice. Generous for one center, tighter when unioning many. */
const SINGLE_CENTER_LIMIT = 500;
const PER_ANSWER_NODE_LIMIT = 150;
/** How many surfaced nodes get their own neighbourhood after a question. */
const MAX_ANSWER_CENTERS = 5;

export function useHorizonGraph(options: HorizonOptions) {
  const { centerId, depth, showAll, surfacedIds, hiddenTypes, sourceFilter } = options;
  const centers = surfacedIds.slice(0, MAX_ANSWER_CENTERS);
  const mode: HorizonMode = showAll || !centerId ? "all" : centers.length > 0 ? "answer" : "center";

  const query = useQuery({
    queryKey: [
      "horizon-graph",
      mode,
      mode === "answer" ? centers.join("|") : centerId,
      depth,
      hiddenTypes.join("|"),
      sourceFilter,
    ],
    enabled: options.enabled ?? true,
    queryFn: async (): Promise<HorizonGraph> => {
      if (mode === "all") {
        const graph = await api.graph({
          excludeTypes: hiddenTypes,
          source: sourceFilter ?? undefined,
        });
        return {
          nodes: graph.nodes,
          links: graph.links,
          depths: {},
          mode,
          matchedNodes: graph.matched_nodes,
        };
      }

      const targets = mode === "answer" ? centers : [centerId as string];
      const limit = mode === "answer" ? PER_ANSWER_NODE_LIMIT : SINGLE_CENTER_LIMIT;
      const slices = await Promise.all(
        targets.map((nodeId) =>
          api
            .graphSlice(nodeId, depth, { limit })
            // One unreachable center must not blank the canvas.
            .catch(() => ({ node_id: nodeId, depth, nodes: [], links: [], depths: {} })),
        ),
      );
      return merge(slices, hiddenTypes, sourceFilter, mode);
    },
  });

  return {
    graph: query.data ?? { nodes: [], links: [], depths: {}, mode },
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    refetch: query.refetch,
    mode,
  };
}

/**
 * Union the slices, then apply the filters the slice endpoint does not know
 * about (hidden node types, source scope) and drop links whose endpoints went
 * with them — a dangling edge renders as a line to nowhere.
 */
function merge(
  slices: Array<{ nodes: GraphNode[]; links: GraphLink[]; depths?: Record<string, number> }>,
  hiddenTypes: string[],
  sourceFilter: string | null,
  mode: HorizonMode,
): HorizonGraph {
  const hidden = new Set(hiddenTypes);
  const nodes = new Map<string, GraphNode>();
  const depths: Record<string, number> = {};
  let matched = 0;

  for (const slice of slices) {
    for (const node of slice.nodes) {
      if (!node.id) continue;
      matched += 1;
      if (node.type && hidden.has(node.type)) continue;
      if (sourceFilter && node.source_id && node.source_id !== sourceFilter) continue;
      nodes.set(node.id, node);
    }
    for (const [nodeId, hop] of Object.entries(slice.depths ?? {})) {
      const current = depths[nodeId];
      if (current === undefined || hop < current) depths[nodeId] = hop;
    }
  }

  const links = new Map<string, GraphLink>();
  for (const slice of slices) {
    for (const link of slice.links) {
      if (!nodes.has(link.source) || !nodes.has(link.target)) continue;
      links.set(`${link.source}|${link.type}|${link.key ?? 0}|${link.target}`, link);
    }
  }

  return {
    nodes: [...nodes.values()],
    links: [...links.values()],
    depths,
    mode,
    matchedNodes: matched,
  };
}
