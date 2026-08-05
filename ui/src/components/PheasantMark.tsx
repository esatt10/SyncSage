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
      <path d="M5.2 10.5 8.4 8.6c.4-2.1 3.4-2.4 4.4-.35.8 1.6.6 3 .25 3.9 3.8-.5 7.2 1.3 8.8 4.05 2 1.35 5 4.7 8.6 10l-1.8 1.15c-3.3-5-6.3-7.05-8.4-7.9-.3 2.3-2.5 3.9-5.5 3.8-3.2-.1-5.1-1.9-5.35-4.4-.27-2.5.42-4.95 1.6-6.2-.7-.65-1.35-1.4-1.7-1.85z" />
    </svg>
  );
}
