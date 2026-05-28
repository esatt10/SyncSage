import type { GraphLink, GraphNode } from "../api/types";

// Cytoscape's exported stylesheet type name varies across @types versions, so we
// keep the stylesheet array loosely typed and let Cytoscape validate at runtime.
type CyStylesheet = Record<string, unknown>;

// Color per node type and dash/colour per edge type so the graph reads as a
// knowledge graph at a glance.
export const NODE_COLORS: Record<string, string> = {
  knowledge_base: "#f5d76e",
  source: "#4dabf7",
  repository: "#4dabf7",
  directory: "#74c0fc",
  file: "#69db7c",
  document: "#38d9a9",
  markdown_note: "#63e6be",
  chunk: "#b197fc",
  symbol: "#ffa94d",
  entity: "#ff8787",
  concept: "#e599f7",
  external_reference: "#ced4da",
};

export const EDGE_COLORS: Record<string, string> = {
  contains: "#495057",
  indexes: "#3b5bdb",
  has_chunk: "#7048e8",
  mentions: "#c2255c",
  derived_from: "#868e96",
  imports: "#e8590c",
  calls: "#d9480f",
  references: "#1098ad",
  similar_to: "#ae3ec9",
  links_to: "#1c7ed6",
};

export const ALL_EDGE_TYPES = Object.keys(EDGE_COLORS);

export function colorForNode(type?: string): string {
  return (type && NODE_COLORS[type]) || "#adb5bd";
}

export interface CyElement {
  data: Record<string, unknown>;
  classes?: string;
}

// Convert a node-link graph into Cytoscape elements, deduplicating ids.
export function toElements(nodes: GraphNode[], links: GraphLink[]): CyElement[] {
  const seen = new Set<string>();
  const elements: CyElement[] = [];
  for (const node of nodes) {
    if (!node.id || seen.has(node.id)) continue;
    seen.add(node.id);
    elements.push({
      data: {
        id: node.id,
        label: shorten(node.label ?? node.id),
        ntype: node.type ?? "unknown",
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

function shorten(label: string): string {
  if (label.length <= 28) return label;
  return `…${label.slice(label.length - 27)}`;
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
        "background-color": "#adb5bd",
        "border-width": 2,
        "border-color": "#adb5bd",
        label: "data(label)",
        color: "#e9ecef",
        "font-size": 9,
        "text-valign": "bottom",
        "text-margin-y": 4,
        "text-wrap": "ellipsis",
        "text-max-width": "120px",
        width: 22,
        height: 22,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1.4,
        "line-color": "#495057",
        "target-arrow-color": "#495057",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.7,
        "curve-style": "bezier",
        opacity: 0.7,
      },
    },
    ...nodeColorRules,
    ...edgeColorRules,
    {
      selector: "node.selected",
      style: { "border-width": 4, "border-color": "#ffd43b", width: 30, height: 30 },
    },
    { selector: ".faded", style: { opacity: 0.12 } },
    {
      selector: "node.focus",
      style: { "border-width": 4, "border-color": "#ffffff" },
    },
  ];
}
