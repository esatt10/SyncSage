import type { Citation } from "../api/types";

/**
 * The innermost section of a breadcrumb — "§ 12.3 Governing Law" out of
 * "MASTER SERVICES AGREEMENT > Article IV … > § 12.3 Governing Law".
 *
 * A chip has room for one short label, and the innermost crumb is the
 * informative one: the ancestors are mostly shared across every hit in the
 * document, so they distinguish nothing here. The full path goes in the
 * tooltip, where there is space for it.
 */
export function sectionLabel(headingPath: string | undefined): string | undefined {
  if (!headingPath) return undefined;
  const crumbs = headingPath.split(">").map((crumb) => crumb.trim()).filter(Boolean);
  return crumbs.length ? crumbs[crumbs.length - 1] : undefined;
}

function chipTooltip(citation: Citation): string {
  const snippet = citation.snippet.slice(0, 300);
  return citation.heading_path ? `${citation.heading_path}\n\n${snippet}` : snippet;
}

/**
 * The citation row under an answer.
 *
 * Cited passages come first and at full strength; retrieved-but-unused ones
 * stay visible at reduced emphasis, because "what did it look at and not use"
 * is as much a part of trusting an answer as "what did it quote".
 *
 * For a source with a taxonomy the chip also names the section the passage came
 * from. On a long structured document — a contract, a standard — the file name
 * alone is nearly no information, since every citation carries the same one.
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
      {ordered.map((citation) => {
        const section = sectionLabel(citation.heading_path);
        return (
          <button
            key={citation.index}
            className={`source-chip${citation.used ? "" : " source-chip--unused"}`}
            onClick={() => onSelect(citation.node_id)}
            title={chipTooltip(citation)}
          >
            <span className="source-chip__n">{citation.index}</span>
            <span className="source-chip__label">
              {citation.memory
                ? (citation.memory.subject ?? "remembered")
                : (citation.relative_path ?? citation.title)}
            </span>
            {/* A reader who cannot tell a remembered assertion from a document
                cannot catch a bad memory — and the path a memory cites is
                `org/mem-2026….md`, which says nothing. */}
            {citation.memory ? (
              <span className="source-chip__memory" title={`asserted ${citation.memory.asserted_at ?? "unknown"}`}>
                {citation.memory.scope ?? "memory"}
              </span>
            ) : null}
            {section ? <span className="source-chip__section">{section}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
