import { useEffect, useMemo, useRef, useState } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import type { Core } from "cytoscape";
import type { GraphLink, GraphNode } from "../api/types";
import { buildStylesheet, type ShapeAlgorithm, toElements } from "./graphStyles";
import { useAppliedTheme } from "../hooks/useTheme";

interface GraphCanvasProps {
  nodes: GraphNode[];
  links: GraphLink[];
  selectedId: string | null;
  focusIds: string[];
  /** Nodes cited by the current answer — outlined so prose maps to graph. */
  citedIds?: string[];
  /** Hop distance from the center, used to ring the canvas by nearness. */
  depths?: Record<string, number>;
  layoutName: string;
  spacing: number;
  shapeAlgorithm: ShapeAlgorithm;
  /** `null` when the background was tapped — a deselect, not a reset. */
  onSelect: (nodeId: string | null) => void;
  /** Double-tap: make this node the center the horizon is measured from. */
  onRecenter?: (nodeId: string) => void;
}

const FORCE_LAYOUT_ELEMENT_LIMIT = 1000;

export function GraphCanvas({
  nodes,
  links,
  selectedId,
  focusIds,
  citedIds = [],
  depths,
  layoutName,
  spacing,
  shapeAlgorithm,
  onSelect,
  onRecenter,
}: GraphCanvasProps) {
  const cyRef = useRef<Core | null>(null);
  const onSelectRef = useRef(onSelect);
  const onRecenterRef = useRef(onRecenter);
  const listenerBoundRef = useRef(false);
  const selectedRef = useRef<string | null>(null);
  const elements = useMemo(
    () => toElements(nodes, links, shapeAlgorithm, depths),
    [nodes, links, shapeAlgorithm, depths],
  );
  const theme = useAppliedTheme();
  const stylesheet = useMemo(() => buildStylesheet(theme), [theme]);
  const layout = useMemo(
    () => layoutOptions(layoutName, spacing, elements.length),
    [layoutName, spacing, elements.length],
  );
  const [layouting, setLayouting] = useState(false);
  const citedKey = citedIds.join("|");

  useEffect(() => {
    onSelectRef.current = onSelect;
    onRecenterRef.current = onRecenter;
  }, [onSelect, onRecenter]);

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
      // cose's own default `numIter` (1200) with `animate: true` (see
      // layoutOptions below) spreads across roughly numIter/refresh ≈ 60
      // requestAnimationFrame-scheduled chunks rather than one blocking
      // call, so this fallback needs enough headroom for that to
      // genuinely finish before it fires — not just cover a fixed
      // worst-case wall-clock budget the way the old synchronous layout's
      // 1400ms guess did.
      window.setTimeout(finish, 8000);
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
        neighborhood.addClass("focus");
      }
    });
  }, [focusIds, elements.length]);

  // Outline the nodes the current answer cited.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.nodes(".cited").removeClass("cited");
      citedIds.forEach((id) => cy.getElementById(id).addClass("cited"));
    });
  }, [citedKey, elements.length, citedIds]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const previous = selectedRef.current;
    if (previous) cy.getElementById(previous).removeClass("selected");
    // Deselecting must not move the camera: the view you were looking at is
    // the thing you were keeping.
    if (selectedId) {
      const node = cy.getElementById(selectedId);
      node.addClass("selected");
      // Bring a node selected from elsewhere (a citation chip, a fact, the
      // source rail) into view rather than silently highlighting off-screen.
      if (node.nonempty()) {
        cy.animate({ center: { eles: node }, duration: 220, easing: "ease-out" });
      }
    }
    selectedRef.current = selectedId;
  }, [selectedId, elements.length]);

  return (
    <div className="graph-canvas-shell">
      {layouting ? <div className="graph-busy">Arranging graph…</div> : null}
      <CytoscapeComponent
        elements={elements as never}
        stylesheet={stylesheet}
        layout={layout as never}
        style={{ width: "100%", height: "100%" }}
        minZoom={0.08}
        maxZoom={3}
        cy={(cy: Core) => {
          cyRef.current = cy;
          if (listenerBoundRef.current) return;
          listenerBoundRef.current = true;
          cy.on("tap", "node", (event) => onSelectRef.current(event.target.id()));
          // Tapping empty canvas clears the selection and nothing else. Before
          // this the only way out of a selection was "Clear", which also reset
          // the depth, the center and the answer filter — so putting a node
          // down meant losing the view you had navigated to.
          cy.on("tap", (event) => {
            if (event.target === cy) onSelectRef.current(null);
          });
          // Double-tap re-aims the horizon at that node — the fastest way to
          // walk a large graph without ever drawing all of it.
          // Both spellings: Cytoscape emits `dblclick` for a mouse and
          // `dbltap` for touch, and the reducer is idempotent if both land.
          cy.on("dblclick", "node", (event) => onRecenterRef.current?.(event.target.id()));
          cy.on("dbltap", "node", (event) => onRecenterRef.current?.(event.target.id()));
        }}
      />
    </div>
  );
}

function layoutOptions(
  layoutName: string,
  spacing: number,
  elementCount: number,
): Record<string, unknown> {
  const name =
    layoutName === "auto"
      ? elementCount > FORCE_LAYOUT_ELEMENT_LIMIT
        ? "concentric"
        : "cose"
      : layoutName;
  const padding = Math.round(40 * spacing);
  if (name === "cose") {
    return {
      name,
      // `animate: false` used to run all `numIter` iterations in one
      // synchronous while-loop (verified against cytoscape's own source —
      // the `else` branch of CoseLayout.prototype.run) with zero yielding
      // back to the browser, which is what froze the rest of the UI while
      // a force layout ran — clicks, chat, other React updates all queue
      // up behind it. `animate: true` runs the identical algorithm through
      // cytoscape's own chunking (`refresh: 20` iterations per
      // requestAnimationFrame call, its default), so the main thread is
      // free between frames. This is "Force" being the default layout now
      // (not just an "auto" fallback for small graphs), so it has to hold
      // up at whatever size a knowledge base actually reaches.
      animate: true,
      // Roomier than the old defaults: the previous values packed labelled
      // nodes close enough that the text overlapped and read as noise.
      nodeRepulsion: Math.round(20000 * spacing),
      idealEdgeLength: Math.round(130 * spacing),
      nodeOverlap: Math.round(24 * spacing),
      gravity: 0.35,
      // Scaled down for large graphs so a legitimately huge horizon still
      // settles in a bounded number of frames — `animate: true` keeps the
      // UI responsive throughout, but "responsive for two minutes" is
      // still worse than "done in a few seconds" when 1200 iterations
      // means 1200 iterations regardless of how many nodes each one costs.
      numIter: elementCount > FORCE_LAYOUT_ELEMENT_LIMIT ? 400 : 1200,
      padding,
    };
  }
  if (name === "concentric") {
    return {
      name,
      animate: false,
      minNodeSpacing: Math.round(44 * spacing),
      padding,
      concentric: (node: { degree: () => number }) => node.degree(),
      levelWidth: () => 2,
    };
  }
  if (name === "breadthfirst") {
    return { name, animate: false, spacingFactor: spacing * 1.4, padding, directed: true };
  }
  return { name, animate: false, spacingFactor: spacing * 1.3, padding };
}
