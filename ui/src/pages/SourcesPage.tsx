import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { SourceRecord } from "../api/types";
import { AddSourceWizard } from "../sources/AddSourceWizard";
import { Explainable } from "../explain/Explainable";

const SYNC_MODES = ["incremental", "full", "validate_only", "repair"];

export function SourcesPage() {
  const queryClient = useQueryClient();
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const [showWizard, setShowWizard] = useState(false);
  const [editingSource, setEditingSource] = useState<SourceRecord | null>(null);
  const [patch, setPatch] = useState<string | null>(null);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["sources"] });
    queryClient.invalidateQueries({ queryKey: ["graph"] });
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
    <div className="page">
      <div className="page__header">
        <h1>Sources</h1>
        <Explainable id="sources.add" as="span">
          <button className="btn btn--primary" onClick={() => setShowWizard(true)}>
            + Add source
          </button>
        </Explainable>
      </div>

      {sources.isLoading && <p className="muted">Loading sources…</p>}
      {sources.isError && <p className="error">{(sources.error as Error).message}</p>}

      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Path</th>
            <th>Status</th>
            <th>Actions</th>
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
              <td>{source.last_status ?? "—"}</td>
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
                <button className="btn btn--small btn--danger" onClick={() => remove.mutate(source.name)}>
                  remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {showWizard && <AddSourceWizard onClose={() => setShowWizard(false)} />}
      {editingSource && (
        <AddSourceWizard source={editingSource} onClose={() => setEditingSource(null)} />
      )}

      {patch && (
        <div className="modal-scrim" onClick={() => setPatch(null)}>
          <div className="modal modal--narrow" onClick={(e) => e.stopPropagation()}>
            <Explainable id="sources.promote">
              <header className="modal__header">
                <h2>YAML patch</h2>
                <button className="btn btn--ghost" onClick={() => setPatch(null)}>
                  ✕
                </button>
              </header>
              <p className="muted small">Add this to your syncsage.yaml to make the source durable.</p>
              <pre className="content-block">{patch}</pre>
            </Explainable>
          </div>
        </div>
      )}
    </div>
  );
}

function SyncControl({ onSync }: { onSync: (mode: string) => void }) {
  const [mode, setMode] = useState("incremental");
  return (
    <Explainable id="sources.syncMode" as="span" className="sync-control">
      <select className="text-input text-input--small" value={mode} onChange={(e) => setMode(e.target.value)}>
        {SYNC_MODES.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <button className="btn btn--small btn--primary" onClick={() => onSync(mode)}>
        sync
      </button>
    </Explainable>
  );
}
