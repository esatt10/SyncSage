/**
 * The pheasant wordmark glyph.
 *
 * Drawn in `currentColor` so it inherits whatever the surface it sits on sets —
 * the top-bar tile fills with `--accent` and flips the glyph colour per theme,
 * and the mark follows without a second copy of those values. Geometry matches
 * `public/favicon.svg`; keep the two in step.
 */
export function PheasantMark({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="currentColor"
      aria-hidden
      focusable="false"
    >
      <path d="M11.2 6.4C10.6 4.8 10 4.1 9.2 3.7c.9 1.2 1.2 2.2 1.15 3.2z" />
      <path d="M13 21.9h.95v3.3H13zM15.8 21.9h.95v3.3H15.8z" />
      <path d="M5 10.35 8.15 8.5c.35-2 3.25-2.3 4.2-.35.75 1.5.6 2.85.25 3.7 3.6-.45 6.8 1.25 8.3 3.85 1.9 1.3 4.7 4.5 8.1 9.5l-1.7 1.1c-3.1-4.5-5.9-6.7-7.9-7.6-.3 2.2-2.4 3.7-5.2 3.6-3-.1-4.8-1.8-5.05-4.2-.25-2.4.4-4.7 1.5-5.9-.65-.6-1.25-1.3-1.6-1.75z" />
    </svg>
  );
}
