import { GRAPH_LAYOUTS, MAX_DEPTH, MIN_DEPTH } from "../state/session";
import type { GraphLayout } from "../state/session";
import type { HorizonMode } from "./useHorizonGraph";

interface HorizonControlsProps {
  mode: HorizonMode;
  depth: number;
  centerLabel: string | null;
  surfacedCount: number;
  nodeCount: number;
  truncated: boolean;
  busy: boolean;
  /** Anything the user has narrowed to: a selection, a center, an answer. */
  dirty: boolean;
  layout: GraphLayout;
  onLayout: (layout: GraphLayout) => void;
  onDepth: (depth: number) => void;
  onShowAll: (value: boolean) => void;
  onClearAnswerFilter: () => void;
  onClear: () => void;
}

/**
 * The horizon controls: how far out from the center the canvas draws, and what
 * the center currently is. Depth is the primary readability lever on a graph
 * with thousands of nodes, so it is a first-class control on the canvas rather
 * than a setting buried in a menu.
 */
export function HorizonControls({
  mode,
  depth,
  layout,
  onLayout,
  centerLabel,
  surfacedCount,
  nodeCount,
  truncated,
  busy,
  dirty,
  onDepth,
  onShowAll,
  onClearAnswerFilter,
  onClear,
}: HorizonControlsProps) {
  const bounded = mode !== "all";
  return (
    <div className="horizon">
      <div className="horizon__row">
        <span className="graph-badge">{nodeCount} nodes</span>
        {truncated ? (
          <span
            className="graph-badge graph-badge--warning"
            title="This view reached its graph safety limit. Recenter the node or use Show all to inspect the wider graph."
          >
            limited view
          </span>
        ) : null}
        {busy ? <span className="graph-badge">loading…</span> : null}
        {mode === "answer" ? (
          <button
            className="graph-badge graph-badge--action"
            onClick={onClearAnswerFilter}
            title="Stop filtering to the last answer and go back to the full horizon"
          >
            {surfacedCount} from answer ✕
          </button>
        ) : null}
        {dirty ? (
          <button
            className="graph-badge graph-badge--action"
            onClick={onClear}
            title="Clear the selected node, the answer filter and the center — back to the plain view"
          >
            Clear ✕
          </button>
        ) : null}
      </div>

      <div className="horizon__row">
        {bounded ? (
          <>
            <span className="horizon__label">Depth</span>
            <button
              className="btn btn--icon btn--small"
              onClick={() => onDepth(depth - 1)}
              disabled={depth <= MIN_DEPTH || busy}
              aria-label="Show one layer less"
              title="One layer less"
            >
              −
            </button>
            <span className="horizon__value" aria-live="polite">
              {depth}
            </span>
            <button
              className="btn btn--icon btn--small"
              onClick={() => onDepth(depth + 1)}
              disabled={depth >= MAX_DEPTH || busy}
              aria-label="Show one layer more"
              title="One layer more"
            >
              +
            </button>
            <button className="btn btn--small" onClick={() => onShowAll(true)} disabled={busy}>
              Show all
            </button>
          </>
        ) : (
          <>
            <span className="horizon__label">Whole graph</span>
            <button className="btn btn--small" onClick={() => onShowAll(false)} disabled={busy}>
              Back to {depth} layers
            </button>
          </>
        )}
        <select
          className="btn btn--small"
          value={layout}
          onChange={(event) => onLayout(event.target.value as GraphLayout)}
          disabled={busy}
          title="How the canvas arranges nodes"
          aria-label="Graph layout"
        >
          {GRAPH_LAYOUTS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      {bounded && centerLabel ? (
        <div className="horizon__row horizon__row--center" title={centerLabel}>
          <span className="horizon__label">Around</span>
          <span className="horizon__center">{centerLabel}</span>
        </div>
      ) : null}
    </div>
  );
}
