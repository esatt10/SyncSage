import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import type { SourceRecord } from "../api/types";
import { QuickAdd } from "./QuickAdd";
import { colorForNode } from "../graph/graphStyles";

/**
 * The Sources rail.
 *
 * Selecting a source scopes both retrieval and the graph to it — the same
 * "which of my sources am I asking about" control, applied once.
 */
export function SourceRail({
  sources,
  selected,
  onSelect,
  onChanged,
  onCollapse,
}: {
  sources: SourceRecord[];
  selected: string | null;
  onSelect: (name: string | null) => void;
  onChanged: () => void;
  onCollapse?: () => void;
}) {
  const [adding, setAdding] = useState(false);

  const sync = useMutation({
    mutationFn: (name: string) => api.syncSource(name, "incremental"),
    onSuccess: onChanged,
  });

  return (
    <>
      <header className="pane__header">
        <span className="pane__title">Sources</span>
        <div className="pane__actions">
          {onCollapse ? (
            <button
              className="btn btn--small btn--icon"
              onClick={onCollapse}
              title="Collapse sources to give the graph and chat more room"
              aria-label="Collapse sources"
            >
              ‹
            </button>
          ) : null}
          <button
            className="btn btn--small btn--primary"
            onClick={() => setAdding(true)}
            title="Add a folder, repo or URL"
          >
            + Add
          </button>
        </div>
      </header>
      <div className="pane__body">
        <div className="source-list">
          <button
            className={`source-item${selected === null ? " active" : ""}`}
            onClick={() => onSelect(null)}
          >
            <div className="source-item__top">
              <span className="source-dot" style={{ background: colorForNode("knowledge_base") }} />
              <span className="source-item__name">All sources</span>
            </div>
            <span className="source-item__meta">{sources.length} indexed</span>
          </button>

          {sources.map((source) => (
            // The row is a plain container holding two siblings — selecting the
            // source and syncing it are separate actions, so nesting the sync
            // button inside the select button would be invalid markup and would
            // give screen readers one control where there are two.
            <div
              key={source.id ?? source.name}
              className={`source-item${selected === source.name ? " active" : ""}`}
            >
              <button
                className="source-item__select"
                onClick={() => onSelect(selected === source.name ? null : source.name)}
                title={source.path}
              >
                <span className="source-item__top">
                  <span className="source-dot" style={{ background: colorForNode("source") }} />
                  <span className="source-item__name">{source.name}</span>
                </span>
                <span className="source-item__meta">
                  <span className="pill">{source.type}</span>
                  {source.syncing ? (
                    <span className="thinking">
                      <span className="spinner" /> syncing…
                    </span>
                  ) : source.sync_error ? (
                    <span className="error" title={source.sync_error}>
                      sync failed
                    </span>
                  ) : source.last_status ? (
                    <span>{source.last_status}</span>
                  ) : null}
                </span>
                <span className="source-item__path">{source.path}</span>
              </button>
              <div className="source-item__actions">
                <button
                  className="btn btn--small btn--ghost"
                  onClick={() => sync.mutate(source.name)}
                  disabled={sync.isPending || Boolean(source.syncing)}
                >
                  {(sync.isPending && sync.variables === source.name) || source.syncing
                    ? "syncing…"
                    : "sync"}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {adding ? (
        <QuickAdd
          onClose={() => setAdding(false)}
          onAdded={() => {
            setAdding(false);
            onChanged();
          }}
        />
      ) : null}
    </>
  );
}
