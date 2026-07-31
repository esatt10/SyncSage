import { Fragment } from "react";
import type { ReactNode } from "react";

/**
 * Renders an answer, turning `[1]` markers into clickable citation chips.
 *
 * The chips are the whole point of the surface: a claim in the prose and the
 * passage it came from are one click apart, and clicking also highlights the
 * cited node on the graph. Markdown is handled with a deliberately small
 * subset (paragraphs, bullets, inline code) rather than a full renderer —
 * grounded answers are short, and a heavyweight renderer would fight the
 * citation splitting for control of the text nodes.
 */
export function AnswerBody({
  text,
  onCite,
}: {
  text: string;
  onCite: (index: number) => void;
}) {
  const blocks = text.split(/\n{2,}/);
  return (
    <>
      {blocks.map((block, blockIndex) => {
        const lines = block.split("\n");
        const isList = lines.every((line) => /^\s*[-*•]\s+/.test(line) || line.trim() === "");
        const isOrdered = lines.every((line) => /^\s*\d+[.)]\s+/.test(line) || line.trim() === "");

        if (isList && lines.some((line) => line.trim())) {
          return (
            <ul key={blockIndex}>
              {lines
                .filter((line) => line.trim())
                .map((line, i) => (
                  <li key={i}>{withCitations(line.replace(/^\s*[-*•]\s+/, ""), onCite)}</li>
                ))}
            </ul>
          );
        }
        if (isOrdered && lines.some((line) => line.trim())) {
          return (
            <ol key={blockIndex}>
              {lines
                .filter((line) => line.trim())
                .map((line, i) => (
                  <li key={i}>{withCitations(line.replace(/^\s*\d+[.)]\s+/, ""), onCite)}</li>
                ))}
            </ol>
          );
        }
        return <p key={blockIndex}>{withCitations(block, onCite)}</p>;
      })}
    </>
  );
}

const CITATION = /\[(\d{1,2})\]/g;
const INLINE_CODE = /`([^`]+)`/g;

function withCitations(text: string, onCite: (index: number) => void): ReactNode[] {
  const out: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  CITATION.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CITATION.exec(text)) !== null) {
    if (match.index > cursor) {
      out.push(<Fragment key={key++}>{withCode(text.slice(cursor, match.index), key)}</Fragment>);
    }
    const index = Number(match[1]);
    out.push(
      <button
        key={key++}
        className="cite-chip"
        onClick={() => onCite(index)}
        title={`Show source ${index}`}
      >
        {index}
      </button>,
    );
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) {
    out.push(<Fragment key={key++}>{withCode(text.slice(cursor), key)}</Fragment>);
  }
  return out;
}

function withCode(text: string, seed: number): ReactNode[] {
  const out: ReactNode[] = [];
  let cursor = 0;
  let key = seed * 1000;
  INLINE_CODE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = INLINE_CODE.exec(text)) !== null) {
    if (match.index > cursor) out.push(text.slice(cursor, match.index));
    out.push(<code key={key++}>{match[1]}</code>);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}
