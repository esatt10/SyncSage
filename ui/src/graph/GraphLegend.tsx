import { ALL_NODE_TYPES, NODE_TYPE_SHAPES, colorForNode } from "./graphStyles";

/**
 * Legend that doubles as the filter.
 *
 * Every entry is a toggle, so hiding the concept haze is one click and the
 * legend never claims to show something the canvas is filtering out. Types
 * absent from the current index are omitted entirely rather than listed dead.
 */
export function GraphLegend({
  counts,
  hidden,
  onToggle,
}: {
  counts: Record<string, number>;
  hidden: string[];
  onToggle: (type: string) => void;
}) {
  const present = ALL_NODE_TYPES.filter((type) => (counts[type] ?? 0) > 0);
  const types = present.length > 0 ? present : ALL_NODE_TYPES.slice(0, 6);
  return (
    <div className="graph-legend">
      {types.map((type) => {
        const off = hidden.includes(type);
        return (
          <button
            key={type}
            className={`legend-entry${off ? " legend-entry--off" : ""}`}
            onClick={() => onToggle(type)}
            title={off ? `Show ${type} nodes` : `Hide ${type} nodes`}
          >
            <span
              className={`legend-swatch legend-swatch--${swatchShape(type)}`}
              style={{ background: colorForNode(type) }}
            />
            {type.replace(/_/g, " ")}
            {counts[type] ? <span className="muted"> {counts[type]}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

function swatchShape(type: string): string {
  const shape = NODE_TYPE_SHAPES[type] ?? "ellipse";
  if (shape === "diamond") return "diamond";
  if (shape === "ellipse") return "circle";
  return "rect";
}
