import type { Citation } from "../api/types";

/**
 * The citation row under an answer.
 *
 * Cited passages come first and at full strength; retrieved-but-unused ones
 * stay visible at reduced emphasis, because "what did it look at and not use"
 * is as much a part of trusting an answer as "what did it quote".
 */
export function SourceStrip({
  citations,
  onSelect,
}: {
  citations: Citation[];
  onSelect: (nodeId: string | undefined) => void;
}) {
  const ordered = [...citations].sort((a, b) => Number(b.used) - Number(a.used) || a.index - b.index);
  return (
    <div className="sources-strip">
      {ordered.map((citation) => (
        <button
          key={citation.index}
          className={`source-chip${citation.used ? "" : " source-chip--unused"}`}
          onClick={() => onSelect(citation.node_id)}
          title={citation.snippet.slice(0, 300)}
        >
          <span className="source-chip__n">{citation.index}</span>
          <span className="source-chip__label">
            {citation.relative_path ?? citation.title}
          </span>
        </button>
      ))}
    </div>
  );
}
