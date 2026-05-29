import { useEffect, useMemo, useRef } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import type { Core } from "cytoscape";
import type { GraphLink, GraphNode } from "../api/types";
import { buildStylesheet, toElements } from "./graphStyles";

interface GraphCanvasProps {
  nodes: GraphNode[];
  links: GraphLink[];
  selectedId: string | null;
  focusIds: string[];
  onSelect: (nodeId: string) => void;
}

const COSE_LAYOUT = {
  name: "cose",
  animate: false,
  nodeRepulsion: 9000,
  idealEdgeLength: 90,
  padding: 30,
} as const;

const FAST_LAYOUT = {
  name: "grid",
  animate: false,
  padding: 30,
} as const;

const FORCE_LAYOUT_ELEMENT_LIMIT = 1000;

export function GraphCanvas({ nodes, links, selectedId, focusIds, onSelect }: GraphCanvasProps) {
  const cyRef = useRef<Core | null>(null);
  const elements = useMemo(() => toElements(nodes, links), [nodes, links]);
  const stylesheet = useMemo(() => buildStylesheet(), []);
  const layout = elements.length > FORCE_LAYOUT_ELEMENT_LIMIT ? FAST_LAYOUT : COSE_LAYOUT;

  // Re-run layout whenever the element set changes (e.g. a sub-network is added).
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.layout(layout).run();
  }, [elements.length, layout]);

  // Apply selection + focus/fade highlighting on top of the rendered graph.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass("selected focus faded");
      if (focusIds.length > 0) {
        const focus = cy.collection();
        focusIds.forEach((id) => focus.merge(cy.getElementById(id)));
        const neighborhood = focus.closedNeighborhood();
        cy.elements().difference(neighborhood).addClass("faded");
        focus.addClass("focus");
      }
      if (selectedId) cy.getElementById(selectedId).addClass("selected");
    });
  }, [selectedId, focusIds, elements.length]);

  return (
    <CytoscapeComponent
      elements={elements as never}
      stylesheet={stylesheet}
      layout={layout as never}
      style={{ width: "100%", height: "100%" }}
      minZoom={0.1}
      maxZoom={3}
      wheelSensitivity={0.2}
      cy={(cy: Core) => {
        cyRef.current = cy;
        cy.removeAllListeners();
        cy.on("tap", "node", (event) => onSelect(event.target.id()));
      }}
    />
  );
}
