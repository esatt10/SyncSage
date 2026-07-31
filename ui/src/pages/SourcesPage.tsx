import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { SourceRecord } from "../api/types";
import { AddSourceWizard } from "../sources/AddSourceWizard";
import { QuickAdd } from "../components/QuickAdd";

const SYNC_MODES = ["incremental", "full", "validate_only", "repair"];

export function SourcesPage() {
  const queryClient = useQueryClient();
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const [quickAdd, setQuickAdd] = useState(false);
  const [showWizard, setShowWizard] = useState(false);
  const [editingSource, setEditingSource] = useState<SourceRecord | null>(null);
  const [patch, setPatch] = useState<string | null>(null);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["sources"] });
    queryClient.invalidateQueries({ queryKey: ["graph"] });
    queryClient.invalidateQueries({ queryKey: ["overview"] });
  };

  const sync = useMutation({
    mutationFn: ({ name, mode }: { name: string; mode: string }) => api.syncSource(name, mode),
    onSuccess: invalidate,
  });
  const disable = useMutation({
    mutationFn: (name: string) => api.disableSource(name),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.removeSource(name),
    onSuccess: invalidate,
  });
  const promote = useMutation({
    mutationFn: (name: string) => api.promoteSource(name, false),
    onSuccess: (data) => setPatch(data.yaml_patch),
  });

  return (
    <div className="page page--wide">
      <div className="page__header">
        <h1>Sources</h1>
        <button className="btn btn--primary" onClick={() => setQuickAdd(true)}>
          + Add source
        </button>
      </div>

      {sources.isLoading && (
        <p className="muted">
          <span className="spinner" /> Loading sources…
        </p>
      )}
      {sources.isError && (
        <div className="banner banner--error">{(sources.error as Error).message}</div>
      )}

      <div className="table-scroll">
        <table className="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Path</th>
            <th>Status</th>
            <th style={{ textAlign: "right" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {sources.data?.map((source: SourceRecord) => (
            <tr key={source.id} className={source.enabled ? "" : "row--disabled"}>
              <td>{source.name}</td>
              <td>
                <span className="pill">{source.type}</span>
              </td>
              <td className="path-cell" title={source.path}>
                {source.path}
              </td>
              <td className="muted small">{source.last_status ?? "—"}</td>
              <td className="actions-cell">
                <SyncControl onSync={(mode) => sync.mutate({ name: source.name, mode })} />
                <button className="btn btn--small" onClick={() => setEditingSource(source)}>
                  edit
                </button>
                <button className="btn btn--small" onClick={() => promote.mutate(source.name)}>
                  promote
                </button>
                <button className="btn btn--small" onClick={() => disable.mutate(source.name)}>
                  disable
                </button>
                <button
                  className="btn btn--small btn--danger"
                  onClick={() => remove.mutate(source.name)}
                >
                  remove
                </button>
              </td>
            </tr>
          ))}
          {sources.data?.length === 0 ? (
            <tr>
              <td colSpan={5} className="muted small" style={{ textAlign: "center" }}>
                No sources yet.
              </td>
            </tr>
          ) : null}
        </tbody>
        </table>
      </div>

      <p className="muted small" style={{ marginTop: 14 }}>
        Need every option (include globs, chunking, branch policy)?{" "}
        <button className="btn btn--small btn--ghost" onClick={() => setShowWizard(true)}>
          Open the advanced form
        </button>
      </p>

      {quickAdd ? (
        <QuickAdd
          onClose={() => setQuickAdd(false)}
          onAdded={() => {
            setQuickAdd(false);
            invalidate();
          }}
        />
      ) : null}
      {showWizard && <AddSourceWizard onClose={() => setShowWizard(false)} />}
      {editingSource && (
        <AddSourceWizard source={editingSource} onClose={() => setEditingSource(null)} />
      )}

      {patch && (
        <div className="modal-scrim" onClick={() => setPatch(null)}>
          <div className="modal modal--narrow" onClick={(e) => e.stopPropagation()}>
            <header className="modal__header">
              <h2>YAML patch</h2>
              <button className="btn btn--ghost btn--icon" onClick={() => setPatch(null)}>
                ✕
              </button>
            </header>
            <p className="muted small">
              Add this to your syncsage.yaml to make the source durable across restarts.
            </p>
            <pre className="content-block">{patch}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

function SyncControl({ onSync }: { onSync: (mode: string) => void }) {
  const [mode, setMode] = useState("incremental");
  return (
    <span className="sync-control">
      <select
        className="text-input text-input--small"
        value={mode}
        onChange={(e) => setMode(e.target.value)}
        style={{ width: "auto" }}
      >
        {SYNC_MODES.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <button className="btn btn--small btn--primary" onClick={() => onSync(mode)}>
        sync
      </button>
    </span>
  );
}
