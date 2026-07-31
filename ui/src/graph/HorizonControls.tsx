import { MAX_DEPTH, MIN_DEPTH } from "../state/session";
import type { HorizonMode } from "./useHorizonGraph";

interface HorizonControlsProps {
  mode: HorizonMode;
  depth: number;
  centerLabel: string | null;
  surfacedCount: number;
  nodeCount: number;
  busy: boolean;
  onDepth: (depth: number) => void;
  onShowAll: (value: boolean) => void;
  onClearAnswerFilter: () => void;
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
  centerLabel,
  surfacedCount,
  nodeCount,
  busy,
  onDepth,
  onShowAll,
  onClearAnswerFilter,
}: HorizonControlsProps) {
  const bounded = mode !== "all";
  return (
    <div className="horizon">
      <div className="horizon__row">
        <span className="graph-badge">{nodeCount} nodes</span>
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
