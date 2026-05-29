import { useEffect, useMemo, useRef, useState } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import type { Core } from "cytoscape";
import type { GraphLink, GraphNode } from "../api/types";
import { buildStylesheet, type ShapeAlgorithm, toElements } from "./graphStyles";

interface GraphCanvasProps {
  nodes: GraphNode[];
  links: GraphLink[];
  selectedId: string | null;
  focusIds: string[];
  layoutName: string;
  spacing: number;
  shapeAlgorithm: ShapeAlgorithm;
  onSelect: (nodeId: string) => void;
}

const FORCE_LAYOUT_ELEMENT_LIMIT = 1000;

export function GraphCanvas({
  nodes,
  links,
  selectedId,
  focusIds,
  layoutName,
  spacing,
  shapeAlgorithm,
  onSelect,
}: GraphCanvasProps) {
  const cyRef = useRef<Core | null>(null);
  const onSelectRef = useRef(onSelect);
  const listenerBoundRef = useRef(false);
  const selectedRef = useRef<string | null>(null);
  const elements = useMemo(() => toElements(nodes, links, shapeAlgorithm), [nodes, links, shapeAlgorithm]);
  const stylesheet = useMemo(() => buildStylesheet(), []);
  const layout = useMemo(
    () => layoutOptions(layoutName, spacing, elements.length),
    [layoutName, spacing, elements.length],
  );
  const [layouting, setLayouting] = useState(false);

  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  // Re-run layout whenever the element set changes (e.g. a sub-network is added).
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    setLayouting(true);
    const handle = window.setTimeout(() => {
      const nextLayout = cy.layout(layout as never);
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        setLayouting(false);
      };
      cy.one("layoutstop", finish);
      nextLayout.run();
      window.setTimeout(finish, 1200);
    }, 40);
    return () => window.clearTimeout(handle);
  }, [elements.length, layout]);

  // Apply focus/fade highlighting only when the focus set changes. Selection is
  // handled separately so a node click does not restyle the entire graph.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass("focus faded");
      if (focusIds.length > 0) {
        const focus = cy.collection();
        focusIds.forEach((id) => focus.merge(cy.getElementById(id)));
        const neighborhood = focus.closedNeighborhood();
        cy.elements().difference(neighborhood).addClass("faded");
        focus.addClass("focus");
      }
    });
  }, [focusIds, elements.length]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const previous = selectedRef.current;
    if (previous) cy.getElementById(previous).removeClass("selected");
    if (selectedId) cy.getElementById(selectedId).addClass("selected");
    selectedRef.current = selectedId;
  }, [selectedId, elements.length]);

  return (
    <div className="graph-canvas-shell">
      {layouting ? <div className="graph-busy">Arranging graph...</div> : null}
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
          if (listenerBoundRef.current) return;
          listenerBoundRef.current = true;
          cy.on("tap", "node", (event) => onSelectRef.current(event.target.id()));
        }}
      />
    </div>
  );
}

function layoutOptions(layoutName: string, spacing: number, elementCount: number): Record<string, unknown> {
  const name = layoutName === "auto"
    ? elementCount > FORCE_LAYOUT_ELEMENT_LIMIT
      ? "grid"
      : "cose"
    : layoutName;
  const padding = Math.round(30 * spacing);
  if (name === "cose") {
    return {
      name,
      animate: false,
      nodeRepulsion: Math.round(9000 * spacing),
      idealEdgeLength: Math.round(90 * spacing),
      padding,
    };
  }
  if (name === "concentric") {
    return { name, animate: false, minNodeSpacing: Math.round(30 * spacing), padding };
  }
  if (name === "breadthfirst") {
    return { name, animate: false, spacingFactor: spacing, padding, directed: true };
  }
  return { name, animate: false, spacingFactor: spacing, padding };
}
