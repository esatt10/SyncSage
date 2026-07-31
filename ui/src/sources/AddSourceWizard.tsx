import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { SourceRecord, SourceWritePayload } from "../api/types";
import { DirectoryBrowser } from "./DirectoryBrowser";

const SOURCE_TYPES = [
  "repository",
  "markdown_folder",
  "obsidian_vault",
  "document_folder",
  "single_file",
  "web_collection",
  "api",
  "s3",
];

const SYNC_MODES = ["incremental", "full", "validate_only", "repair"];

interface AddSourceWizardProps {
  source?: SourceRecord | null;
  onClose: () => void;
}

export function AddSourceWizard({ source, onClose }: AddSourceWizardProps) {
  const queryClient = useQueryClient();
  const editing = Boolean(source);
  const initial = useMemo(() => sourceConfig(source), [source]);
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string | null>((initial.path as string | undefined) ?? null);
  const [name, setName] = useState(String(initial.name ?? ""));
  const [type, setType] = useState(String(initial.type ?? "document_folder"));
  const [description, setDescription] = useState(String(initial.description ?? ""));
  const [enabled, setEnabled] = useState(Boolean(initial.enabled ?? true));
  const [maxDepth, setMaxDepth] = useState(
    initial.max_depth === null || initial.max_depth === undefined ? "" : String(initial.max_depth),
  );
  const [includeText, setIncludeText] = useState(lines(initial.include));
  const [excludeText, setExcludeText] = useState(lines(initial.exclude));
  const chunking = (initial.chunking ?? {}) as Record<string, unknown>;
  const repo = (initial.repo ?? {}) as Record<string, unknown>;
  const sync = (initial.sync ?? {}) as Record<string, unknown>;
  const connector = (initial.connector ?? {}) as Record<string, unknown>;
  const [chunkingEnabled, setChunkingEnabled] = useState(Boolean(chunking.enabled ?? true));
  const [chunkStrategy, setChunkStrategy] = useState(String(chunking.strategy ?? "semantic"));
  const [chunkMax, setChunkMax] = useState(String(chunking.max_chars ?? 4000));
  const [chunkOverlap, setChunkOverlap] = useState(String(chunking.overlap_chars ?? 400));
  const [repoBranchPolicy, setRepoBranchPolicy] = useState(String(repo.branch_policy ?? "current"));
  const [repoIncludeUncommitted, setRepoIncludeUncommitted] = useState(
    Boolean(repo.include_uncommitted ?? true),
  );
  const [repoCommitTrigger, setRepoCommitTrigger] = useState(Boolean(repo.commit_trigger ?? true));
  const [syncOnStartup, setSyncOnStartup] = useState(Boolean(sync.on_startup ?? true));
  const [syncOnFileChange, setSyncOnFileChange] = useState(String(sync.on_file_change ?? "debounce"));
  const [syncOnGitCommit, setSyncOnGitCommit] = useState(Boolean(sync.on_git_commit ?? true));
  const [syncInterval, setSyncInterval] = useState(
    sync.interval_seconds === null || sync.interval_seconds === undefined
      ? ""
      : String(sync.interval_seconds),
  );
  const [urlsText, setUrlsText] = useState(lines(initial.urls));
  const [connectorText, setConnectorText] = useState(JSON.stringify(connector, null, 2));
  const [syncNow, setSyncNow] = useState(!editing);
  const [syncMode, setSyncMode] = useState("incremental");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async () => {
      const payload = buildPayload();
      if (!editing) {
        return api.registerSource(payload as SourceWritePayload & { name: string; path: string });
      }
      const result = await api.updateSource(source!.name, payload);
      // Updating a source clears its indexed artifacts server-side, so kick
      // off an incremental re-sync; otherwise the graph stays empty until the
      // user remembers to sync manually. Failures surface on the sources page
      // via last_status rather than blocking the save.
      api
        .syncSource(source!.name, "incremental")
        .catch(() => undefined)
        .finally(() => {
          queryClient.invalidateQueries({ queryKey: ["sources"] });
          queryClient.invalidateQueries({ queryKey: ["graph"] });
        });
      return result;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
      queryClient.invalidateQueries({ queryKey: ["graph"] });
      onClose();
    },
    onError: (err) => setError((err as Error).message),
  });

  const choose = (path: string) => {
    setChosen(path);
    if (!name) {
      const leaf = path.split(/[\\/]/).filter(Boolean).pop() ?? "source";
      setName(leaf.replace(/[^a-zA-Z0-9-_]/g, "-").toLowerCase());
    }
  };

  const buildPayload = (): SourceWritePayload & { name?: string; path?: string } => {
    setError(null);
    let connectorPayload: Record<string, unknown>;
    try {
      connectorPayload = JSON.parse(connectorText || "{}");
    } catch {
      throw new Error("Connector settings must be valid JSON.");
    }
    const payload: SourceWritePayload = {
      type,
      path: chosen ?? undefined,
      description: description || undefined,
      enabled,
      max_depth: maxDepth.trim() ? Number(maxDepth) : null,
      // On create, omit empty pattern lists so the server's defaults apply
      // (curated text-file includes, .git/node_modules excludes). Sending []
      // would override those defaults and index everything. On edit the
      // fields are prefilled from the existing config, so send them as-is —
      // clearing them there is an explicit choice.
      include: editing ? patterns(includeText) : orUndefined(patterns(includeText)),
      exclude: editing ? patterns(excludeText) : orUndefined(patterns(excludeText)),
      chunking: {
        enabled: chunkingEnabled,
        strategy: chunkStrategy,
        max_chars: Number(chunkMax),
        overlap_chars: Number(chunkOverlap),
      },
      repo: {
        branch_policy: repoBranchPolicy,
        include_uncommitted: repoIncludeUncommitted,
        commit_trigger: repoCommitTrigger,
      },
      sync: {
        on_startup: syncOnStartup,
        on_file_change: syncOnFileChange,
        on_git_commit: syncOnGitCommit,
        interval_seconds: syncInterval.trim() ? Number(syncInterval) : null,
      },
      connector: connectorPayload,
      urls: patterns(urlsText),
    };
    if (!editing) {
      payload.name = name;
      payload.sync_now = syncNow;
      payload.sync_mode = syncMode;
    }
    return payload;
  };

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <header className="modal__header">
          <h2>{editing ? "Edit source" : "Add a source"}</h2>
          <button className="btn btn--ghost" onClick={onClose}>
            close
          </button>
        </header>

        <div className="wizard">
          <div className="wizard__col">
            <h4>Directory</h4>
            <DirectoryBrowser
              path={browsePath}
              onNavigate={setBrowsePath}
              onChoose={choose}
              allowFiles={type === "single_file"}
            />
          </div>

          <div className="wizard__col wizard__col--form">
            <h4>Source settings</h4>
            <div className="form-grid">
              <label className="field">
                <span>Chosen path</span>
                <input
                  className="text-input"
                  value={chosen ?? ""}
                  onChange={(event) => setChosen(event.target.value)}
                  placeholder="Container-visible path"
                />
              </label>
              <label className="field">
                <span>Name</span>
                <input
                  className="text-input"
                  value={name}
                  disabled={editing}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>Type</span>
                <select className="text-input" value={type} onChange={(e) => setType(e.target.value)}>
                  {SOURCE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Folder depth</span>
                <input
                  className="text-input"
                  type="number"
                  min="0"
                  value={maxDepth}
                  onChange={(event) => setMaxDepth(event.target.value)}
                  placeholder="unlimited"
                />
              </label>
            </div>

            <label className="field">
              <span>Description</span>
              <input
                className="text-input"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>

            <div className="form-grid">
              <label className="field">
                <span>Include patterns</span>
                <textarea
                  className="text-area"
                  value={includeText}
                  onChange={(e) => setIncludeText(e.target.value)}
                  placeholder={editing ? undefined : "empty = server defaults (**/*.py, **/*.md, …)"}
                />
              </label>
              <label className="field">
                <span>Exclude patterns</span>
                <textarea
                  className="text-area"
                  value={excludeText}
                  onChange={(e) => setExcludeText(e.target.value)}
                  placeholder={editing ? undefined : "empty = server defaults (.git, node_modules, …)"}
                />
              </label>
            </div>

            <details className="settings-group" open>
              <summary>Indexing</summary>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={chunkingEnabled}
                  onChange={(e) => setChunkingEnabled(e.target.checked)}
                />
                Chunk content for retrieval
              </label>
              <div className="form-grid">
                <label className="field">
                  <span>Chunk strategy</span>
                  <input className="text-input" value={chunkStrategy} onChange={(e) => setChunkStrategy(e.target.value)} />
                </label>
                <label className="field">
                  <span>Max chars</span>
                  <input className="text-input" type="number" value={chunkMax} onChange={(e) => setChunkMax(e.target.value)} />
                </label>
                <label className="field">
                  <span>Overlap chars</span>
                  <input className="text-input" type="number" value={chunkOverlap} onChange={(e) => setChunkOverlap(e.target.value)} />
                </label>
              </div>
            </details>

            <details className="settings-group">
              <summary>Sync and connectors</summary>
              <div className="form-grid">
                <label className="checkbox">
                  <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                  Enabled
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={syncOnStartup} onChange={(e) => setSyncOnStartup(e.target.checked)} />
                  Sync on startup
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={syncOnGitCommit} onChange={(e) => setSyncOnGitCommit(e.target.checked)} />
                  Sync on git commit
                </label>
                <label className="field">
                  <span>File changes</span>
                  <input className="text-input" value={syncOnFileChange} onChange={(e) => setSyncOnFileChange(e.target.value)} />
                </label>
                <label className="field">
                  <span>Interval seconds</span>
                  <input className="text-input" type="number" value={syncInterval} onChange={(e) => setSyncInterval(e.target.value)} />
                </label>
              </div>
              <label className="field">
                <span>URLs</span>
                <textarea className="text-area" value={urlsText} onChange={(e) => setUrlsText(e.target.value)} />
              </label>
              <label className="field">
                <span>Connector JSON</span>
                <textarea className="text-area text-area--code" value={connectorText} onChange={(e) => setConnectorText(e.target.value)} />
              </label>
            </details>

            <details className="settings-group">
              <summary>Repository</summary>
              <div className="form-grid">
                <label className="field">
                  <span>Branch policy</span>
                  <input className="text-input" value={repoBranchPolicy} onChange={(e) => setRepoBranchPolicy(e.target.value)} />
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={repoIncludeUncommitted} onChange={(e) => setRepoIncludeUncommitted(e.target.checked)} />
                  Include uncommitted
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={repoCommitTrigger} onChange={(e) => setRepoCommitTrigger(e.target.checked)} />
                  Commit trigger
                </label>
              </div>
            </details>

            {!editing && (
              <div className="form-grid">
                <label className="checkbox">
                  <input type="checkbox" checked={syncNow} onChange={(e) => setSyncNow(e.target.checked)} />
                  Sync now
                </label>
                <label className="field">
                  <span>Sync mode</span>
                  <select className="text-input" value={syncMode} onChange={(e) => setSyncMode(e.target.value)}>
                    {SYNC_MODES.map((mode) => (
                      <option key={mode} value={mode}>
                        {mode}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}

            {(error || mutation.isError) && <p className="error">{error ?? (mutation.error as Error).message}</p>}

            <button
              className="btn btn--primary"
              disabled={!chosen || !name || mutation.isPending}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? "Saving..." : editing ? "Save source" : "Register source"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function sourceConfig(source?: SourceRecord | null): Record<string, unknown> {
  if (!source) return {};
  if (source.config_json) {
    try {
      return JSON.parse(source.config_json);
    } catch {
      return source;
    }
  }
  return source;
}

function orUndefined(value: string[]): string[] | undefined {
  return value.length > 0 ? value : undefined;
}

function patterns(value: string): string[] {
  return value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function lines(value: unknown): string {
  return Array.isArray(value) ? value.join("\n") : "";
}
