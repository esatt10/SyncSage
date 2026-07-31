import type { GraphLink, GraphNode } from "../api/types";

// Cytoscape's exported stylesheet type name varies across @types versions, so we
// keep the stylesheet array loosely typed and let Cytoscape validate at runtime.
type CyStylesheet = Record<string, unknown>;

/**
 * Node colours, tuned for a light canvas.
 *
 * The old palette was picked for a near-black background and washed out on
 * paper; these are the same hue families at a darker, higher-contrast value so
 * type is still readable as colour at a glance on either theme.
 */
export const NODE_COLORS: Record<string, string> = {
  knowledge_base: "#8a6100",
  source: "#1f6f8b",
  repository: "#1f6f8b",
  directory: "#4a8fa8",
  file: "#2f6f4f",
  document: "#2f6f4f",
  markdown_note: "#3d8a63",
  chunk: "#7b6cc4",
  symbol: "#b5651d",
  entity: "#b3475c",
  concept: "#8a4fb0",
  external_reference: "#7a766e",
};

export const EDGE_COLORS: Record<string, string> = {
  contains: "#9d998f",
  indexes: "#7d8dae",
  has_chunk: "#8d80b8",
  mentions: "#b57f96",
  derived_from: "#9d998f",
  imports: "#c08a4e",
  calls: "#b87742",
  references: "#5d97a6",
  similar_to: "#a684bd",
  links_to: "#6892b2",
};

export const ALL_EDGE_TYPES = Object.keys(EDGE_COLORS);
export const ALL_NODE_TYPES = Object.keys(NODE_COLORS);

/**
 * Types hidden by default.
 *
 * A real index produces far more `concept` nodes than anything else — on the
 * sample workspace they are ~80% of the graph — and drawn all at once they bury
 * the structure the user actually came to navigate. They stay one click away in
 * the legend rather than being the first thing you see.
 */
export const NOISY_NODE_TYPES = ["concept", "chunk"];

export type ShapeAlgorithm = "node_type" | "degree" | "uniform";

/**
 * Four shapes, not twelve.
 *
 * Cytoscape offers stars, vees and tags, and using all of them made the canvas
 * read as decoration rather than structure. The vocabulary here is deliberately
 * small: containers are rounded rectangles, documents are rectangles, ideas are
 * circles, and code symbols are diamonds. Colour carries the finer distinction.
 */
export const NODE_TYPE_SHAPES: Record<string, string> = {
  knowledge_base: "round-rectangle",
  source: "round-rectangle",
  repository: "round-rectangle",
  directory: "round-rectangle",
  file: "rectangle",
  document: "rectangle",
  markdown_note: "rectangle",
  chunk: "ellipse",
  symbol: "diamond",
  entity: "ellipse",
  concept: "ellipse",
  external_reference: "rectangle",
};

/** Base radius per type, so the graph has a visual hierarchy at rest. */
const NODE_SIZES: Record<string, number> = {
  knowledge_base: 46,
  source: 36,
  repository: 36,
  directory: 24,
  file: 26,
  document: 26,
  markdown_note: 26,
  chunk: 14,
  symbol: 18,
  entity: 18,
  concept: 14,
  external_reference: 16,
};

export function colorForNode(type?: string): string {
  return (type && NODE_COLORS[type]) || "#7a766e";
}

export function shapeForNodeType(type?: string): string {
  return (type && NODE_TYPE_SHAPES[type]) || "ellipse";
}

export function sizeForNodeType(type?: string): number {
  return (type && NODE_SIZES[type]) || 20;
}

export interface CyElement {
  data: Record<string, unknown>;
  classes?: string;
}

// Convert a node-link graph into Cytoscape elements, deduplicating ids.
export function toElements(
  nodes: GraphNode[],
  links: GraphLink[],
  shapeAlgorithm: ShapeAlgorithm = "node_type",
): CyElement[] {
  const seen = new Set<string>();
  const elements: CyElement[] = [];
  const degree = new Map<string, number>();
  links.forEach((link) => {
    degree.set(link.source, (degree.get(link.source) ?? 0) + 1);
    degree.set(link.target, (degree.get(link.target) ?? 0) + 1);
  });
  for (const node of nodes) {
    if (!node.id || seen.has(node.id)) continue;
    seen.add(node.id);
    const connections = degree.get(node.id) ?? 0;
    elements.push({
      data: {
        id: node.id,
        label: shorten(node.label ?? node.id),
        ntype: node.type ?? "unknown",
        shape: shapeForNode(node.type, connections, shapeAlgorithm),
        // Well-connected nodes grow a little so hubs stand out without a
        // separate "importance" legend.
        size: sizeForNode(node.type, connections, shapeAlgorithm),
      },
    });
  }
  for (const link of links) {
    if (!seen.has(link.source) || !seen.has(link.target)) continue;
    const etype = link.type ?? "related";
    const id = `${link.source}__${etype}__${link.key ?? 0}__${link.target}`;
    elements.push({
      data: { id, source: link.source, target: link.target, etype, label: etype },
    });
  }
  return elements;
}

function shapeForNode(type: string | undefined, degree: number, algorithm: ShapeAlgorithm): string {
  if (algorithm === "uniform") return "ellipse";
  if (algorithm === "degree") {
    if (degree >= 8) return "round-rectangle";
    if (degree >= 3) return "diamond";
    return "ellipse";
  }
  return shapeForNodeType(type);
}

function sizeForNode(type: string | undefined, degree: number, algorithm: ShapeAlgorithm): number {
  if (algorithm === "degree") return Math.min(52, 14 + degree * 3);
  const base = sizeForNodeType(type);
  return Math.min(base + Math.min(degree, 12), base * 1.8);
}

function shorten(label: string): string {
  if (label.length <= 26) return label;
  return `…${label.slice(label.length - 25)}`;
}

export function buildStylesheet(): CyStylesheet[] {
  const nodeColorRules: CyStylesheet[] = Object.entries(NODE_COLORS).map(([type, color]) => ({
    selector: `node[ntype = "${type}"]`,
    style: { "background-color": color, "border-color": color },
  }));
  const edgeColorRules: CyStylesheet[] = Object.entries(EDGE_COLORS).map(([type, color]) => ({
    selector: `edge[etype = "${type}"]`,
    style: { "line-color": color, "target-arrow-color": color },
  }));
  return [
    {
      selector: "node",
      style: {
        "background-color": "#7a766e",
        "background-opacity": 0.92,
        shape: "data(shape)",
        "border-width": 0,
        label: "data(label)",
        // Read against the canvas, not against the node — labels sit below.
        color: "var(--graph-label, #4a4741)",
        "font-size": 9.5,
        "font-weight": 500,
        "text-valign": "bottom",
        "text-margin-y": 5,
        "text-wrap": "ellipsis",
        "text-max-width": "110px",
        "text-background-color": "#ffffff",
        "text-background-opacity": 0,
        width: "data(size)",
        height: "data(size)",
        "transition-property": "opacity, border-width, background-opacity",
        "transition-duration": 120,
      },
    },
    // Only the structural spine is labelled at rest; labelling every concept
    // turns the canvas into a word cloud.
    {
      selector: 'node[ntype = "concept"], node[ntype = "chunk"]',
      style: { "font-size": 8.5, opacity: 0.8 },
    },
    {
      selector: 'node[ntype = "knowledge_base"], node[ntype = "source"]',
      style: { "font-size": 11.5, "font-weight": 600 },
    },
    {
      selector: "edge",
      style: {
        width: 1.1,
        "line-color": "#a9a59c",
        "target-arrow-color": "#a9a59c",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.6,
        "curve-style": "bezier",
        opacity: 0.75,
      },
    },
    ...nodeColorRules,
    ...edgeColorRules,
    {
      selector: "node.selected",
      style: {
        "border-width": 3,
        "border-color": "#1c1b19",
        "border-opacity": 0.85,
        "background-opacity": 1,
        "z-index": 20,
      },
    },
    {
      // Nodes the current answer cited — the link from prose back to graph.
      selector: "node.cited",
      style: {
        "border-width": 3,
        "border-color": "#2f6f4f",
        "background-opacity": 1,
        "z-index": 15,
      },
    },
    // Faded, not erased: the surrounding structure is still the context that
    // makes a focused neighbourhood legible.
    { selector: ".faded", style: { opacity: 0.18 } },
    {
      selector: "node.focus",
      style: { "border-width": 2, "border-color": "#2f6f4f", "z-index": 10 },
    },
    { selector: "edge.focus", style: { opacity: 0.9, width: 1.6 } },
  ];
}
