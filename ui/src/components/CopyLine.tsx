import { useState } from "react";

/** A shell command with a copy button. */
export function CopyLine({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code-line">
      <code>{command}</code>
      <button
        className="btn btn--small btn--ghost"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1600);
          } catch {
            /* clipboard blocked (insecure origin) — the text is selectable */
          }
        }}
      >
        {copied ? "copied" : "copy"}
      </button>
    </div>
  );
}
