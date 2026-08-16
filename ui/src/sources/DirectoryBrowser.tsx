import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

interface DirectoryBrowserProps {
  path: string | null;
  onNavigate: (path: string | null) => void;
  onChoose: (path: string) => void;
  // When true (single_file sources), files are selectable too, not just dirs.
  allowFiles?: boolean;
}

// Server-side validation decides which container-visible paths may be opened.
export function DirectoryBrowser({ path, onNavigate, onChoose, allowFiles = false }: DirectoryBrowserProps) {
  const [manualPath, setManualPath] = useState("");
  const listing = useQuery({
    queryKey: ["fs", path],
    queryFn: () => api.fsList(path ?? undefined),
  });

  return (
    <div className="dir-browser">
      <div className="dir-browser__bar">
        <span className="muted small">{listing.data?.path ?? "Allowlisted roots"}</span>
        {listing.data?.parent && (
          <button className="btn btn--ghost" onClick={() => onNavigate(listing.data!.parent)}>
            ↑ up
          </button>
        )}
        {path && (
          <button className="btn btn--ghost" onClick={() => onNavigate(null)}>
            ⌂ roots
          </button>
        )}
      </div>
      <form
        className="dir-browser__open"
        onSubmit={(event) => {
          event.preventDefault();
          if (manualPath.trim()) onNavigate(manualPath.trim());
        }}
      >
        <input
          className="text-input"
          value={manualPath}
          onChange={(event) => setManualPath(event.target.value)}
          placeholder="Open a container-visible path, e.g. /workspace or /exports"
        />
        <button className="btn btn--small" type="submit">
          open
        </button>
      </form>
      {listing.isError && <p className="error">{(listing.error as Error).message}</p>}
      <ul className="dir-list">
        {listing.data?.entries.map((entry) => (
          <li key={entry.path} className="dir-row">
            <button
              className="dir-row__name"
              disabled={!entry.is_dir}
              onClick={() => entry.is_dir && onNavigate(entry.path)}
            >
              {entry.is_dir ? "📁" : "📄"} {entry.name}
              {entry.mounted === false && <span className="muted small"> (not mounted)</span>}
            </button>
            {(entry.is_dir || allowFiles) && (
              <button className="btn btn--small" onClick={() => onChoose(entry.path)}>
                choose
              </button>
            )}
          </li>
        ))}
      </ul>
      {listing.data?.path && (
        <button className="btn btn--primary" onClick={() => onChoose(listing.data!.path!)}>
          Use this directory
        </button>
      )}
    </div>
  );
}
