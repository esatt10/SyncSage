import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Explainable } from "../explain/Explainable";

interface DirectoryBrowserProps {
  path: string | null;
  onNavigate: (path: string | null) => void;
  onChoose: (path: string) => void;
}

// Allowlist-scoped directory browser. The server only ever returns paths under
// SyncSage's configured roots, so the UI cannot reach anything outside them.
export function DirectoryBrowser({ path, onNavigate, onChoose }: DirectoryBrowserProps) {
  const listing = useQuery({
    queryKey: ["fs", path],
    queryFn: () => api.fsList(path ?? undefined),
  });

  return (
    <Explainable id="sources.directory" className="dir-browser">
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
            </button>
            {entry.is_dir && (
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
    </Explainable>
  );
}
