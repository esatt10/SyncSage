import { Explainable } from "../explain/Explainable";

interface BreadcrumbsProps {
  trail: { id: string; label: string }[];
  onJump: (index: number) => void;
}

// The traversal path through abstraction levels; click an ancestor to step back.
export function Breadcrumbs({ trail, onJump }: BreadcrumbsProps) {
  if (trail.length === 0) return null;
  return (
    <Explainable id="graph.breadcrumbs" className="breadcrumbs">
      {trail.map((crumb, index) => (
        <span key={crumb.id} className="breadcrumbs__item">
          <button className="breadcrumbs__link" onClick={() => onJump(index)}>
            {crumb.label}
          </button>
          {index < trail.length - 1 && <span className="breadcrumbs__sep">›</span>}
        </span>
      ))}
    </Explainable>
  );
}
