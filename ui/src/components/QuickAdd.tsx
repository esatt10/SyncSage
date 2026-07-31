import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import type { QuickAddResponse } from "../api/types";

const EXAMPLES = [
  { label: "A folder", value: "/workspace/notes" },
  { label: "A git repo", value: "https://github.com/owner/repo" },
  { label: "A docs site", value: "https://docs.example.com/guide" },
  { label: "Every subfolder", value: "/workspace/clients/*" },
];

/**
 * One field, anything in it.
 *
 * This is the UI half of `syncsage up <target>`: the server does the same
 * detection (folder vs vault vs repo vs URL vs bucket vs connector), clones
 * what needs cloning, registers the source and syncs it. The user picks
 * nothing — no type dropdown, no include globs, no wizard steps.
 */
export function QuickAdd({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [target, setTarget] = useState("");
  const [split, setSplit] = useState(false);
  const [result, setResult] = useState<QuickAddResponse | null>(null);

  const add = useMutation({
    mutationFn: () => api.quickAdd({ target: target.trim(), split, sync_now: true }),
    onSuccess: (data) => setResult(data),
  });

  const indexed = (result?.sync_results ?? []).reduce(
    (total, entry) => total + (entry.indexed_artifacts ?? 0),
    0,
  );

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <header className="modal__header">
          <h2>Add a source</h2>
          <button className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {result ? (
          <>
            <div className="banner banner--info">
              Added {result.sources.length} source{result.sources.length === 1 ? "" : "s"} and
              indexed {indexed} file{indexed === 1 ? "" : "s"}.
            </div>
            <table className="data-table">
              <tbody>
                {result.sources.map((source, index) => (
                  <tr key={source.name}>
                    <td>{source.name}</td>
                    <td>
                      <span className="pill">{source.type}</span>
                    </td>
                    <td className="path-cell" title={source.path}>
                      {source.path}
                    </td>
                    <td>{result.sync_results[index]?.error ? "failed" : "indexed"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="modal__footer">
              <button className="btn btn--primary" onClick={onAdded}>
                Done
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="form-grid">
              <label className="field">
                <span>Paste a path, URL, glob or connector</span>
                <input
                  className="text-input"
                  autoFocus
                  spellCheck={false}
                  placeholder="/workspace/notes  ·  https://github.com/owner/repo  ·  notion:workspace"
                  value={target}
                  onChange={(event) => setTarget(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && target.trim()) add.mutate();
                  }}
                />
              </label>

              <div className="sources-strip">
                {EXAMPLES.map((example) => (
                  <button
                    key={example.value}
                    className="source-chip"
                    onClick={() => setTarget(example.value)}
                  >
                    <span className="source-chip__label">
                      {example.label}: {example.value}
                    </span>
                  </button>
                ))}
              </div>

              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={split}
                  onChange={(event) => setSplit(event.target.checked)}
                />
                Index each subfolder as its own source
              </label>

              <p className="muted small" style={{ margin: 0 }}>
                SyncSage detects what it is — repository, Obsidian vault, notes folder,
                document folder, web collection, bucket or connector — then indexes it.
                The equivalent command is{" "}
                <code>syncsage up {target.trim() || "<target>"}</code>.
              </p>

              {add.isError ? (
                <div className="banner banner--error" style={{ marginBottom: 0 }}>
                  {(add.error as Error).message}
                </div>
              ) : null}
            </div>

            <div className="modal__footer">
              <button className="btn" onClick={onClose}>
                Cancel
              </button>
              <button
                className="btn btn--primary"
                onClick={() => add.mutate()}
                disabled={!target.trim() || add.isPending}
              >
                {add.isPending ? "Indexing…" : "Add and index"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
