import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { DirectoryBrowser } from "./DirectoryBrowser";
import { Explainable } from "../explain/Explainable";

const SOURCE_TYPES = [
  "repository",
  "markdown_folder",
  "obsidian_vault",
  "document_folder",
  "single_file",
  "web_collection",
];

interface AddSourceWizardProps {
  onClose: () => void;
}

// Open a local directory and register it as a source; optionally sync immediately
// so it appears on the knowledge graph.
export function AddSourceWizard({ onClose }: AddSourceWizardProps) {
  const queryClient = useQueryClient();
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [type, setType] = useState("document_folder");
  const [description, setDescription] = useState("");
  const [syncNow, setSyncNow] = useState(true);

  const register = useMutation({
    mutationFn: () =>
      api.registerSource({
        name,
        type,
        path: chosen as string,
        description: description || undefined,
        sync_now: syncNow,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
      queryClient.invalidateQueries({ queryKey: ["graph"] });
      onClose();
    },
  });

  const choose = (path: string) => {
    setChosen(path);
    if (!name) {
      const leaf = path.split("/").filter(Boolean).pop() ?? "source";
      setName(leaf.replace(/[^a-zA-Z0-9-_]/g, "-").toLowerCase());
    }
  };

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal__header">
          <h2>Add a source</h2>
          <button className="btn btn--ghost" onClick={onClose}>
            ✕
          </button>
        </header>

        <div className="wizard">
          <div className="wizard__col">
            <h4>1 · Open a directory</h4>
            <DirectoryBrowser path={browsePath} onNavigate={setBrowsePath} onChoose={choose} />
          </div>

          <div className="wizard__col">
            <h4>2 · Configure</h4>
            <label className="field">
              <span>Chosen path</span>
              <input className="text-input" value={chosen ?? ""} readOnly placeholder="select a directory →" />
            </label>
            <label className="field">
              <span>Name</span>
              <input className="text-input" value={name} onChange={(e) => setName(e.target.value)} />
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
              <span>Description</span>
              <input
                className="text-input"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <Explainable id="sources.syncMode" as="span">
              <label className="checkbox">
                <input type="checkbox" checked={syncNow} onChange={(e) => setSyncNow(e.target.checked)} />
                Sync now (renders on the graph immediately)
              </label>
            </Explainable>

            {register.isError && <p className="error">{(register.error as Error).message}</p>}

            <button
              className="btn btn--primary"
              disabled={!chosen || !name || register.isPending}
              onClick={() => register.mutate()}
            >
              {register.isPending ? "Registering…" : "Register source"}
            </button>
            <p className="muted small">
              Registered at runtime. Use “Promote to YAML” afterwards to persist it.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
