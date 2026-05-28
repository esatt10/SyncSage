import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import yaml from "js-yaml";
import { api } from "../api/client";
import { ObjectEditor } from "../config/ObjectEditor";
import { lineDiff } from "../util/diff";
import { Explainable } from "../explain/Explainable";

type Mode = "form" | "yaml";

export function ConfigPage() {
  const queryClient = useQueryClient();
  const configQuery = useQuery({ queryKey: ["config"], queryFn: api.getConfig });
  const [mode, setMode] = useState<Mode>("form");
  const [working, setWorking] = useState<Record<string, unknown>>({});
  const [yamlText, setYamlText] = useState("");
  const [showDiff, setShowDiff] = useState(false);

  useEffect(() => {
    if (configQuery.data) {
      setWorking(structuredClone(configQuery.data.effective));
      setYamlText(configQuery.data.raw_yaml ?? yaml.dump(configQuery.data.effective));
    }
  }, [configQuery.data]);

  // The proposed YAML always reflects whichever editor the user is using.
  const proposedYaml = useMemo(() => {
    if (mode === "yaml") return yamlText;
    try {
      return yaml.dump(working, { sortKeys: false });
    } catch {
      return yamlText;
    }
  }, [mode, yamlText, working]);

  const original = configQuery.data?.raw_yaml ?? "";
  const diff = useMemo(() => lineDiff(original, proposedYaml), [original, proposedYaml]);
  const changedCount = diff.filter((d) => d.kind !== "same").length;

  const apply = useMutation({
    mutationFn: () =>
      mode === "yaml"
        ? api.putConfig({ yaml_text: yamlText })
        : api.putConfig({ config: working }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["config"] }),
  });

  if (configQuery.isLoading) return <div className="page"><p className="muted">Loading config…</p></div>;
  if (configQuery.isError)
    return <div className="page"><p className="error">{(configQuery.error as Error).message}</p></div>;

  return (
    <div className="page">
      <div className="page__header">
        <h1>Configuration</h1>
        <div className="muted small">{configQuery.data?.path}</div>
      </div>

      <div className="config-toolbar">
        <div className="tabs">
          <button className={mode === "form" ? "tab active" : "tab"} onClick={() => setMode("form")}>
            Form
          </button>
          <Explainable id="config.rawYaml" as="span">
            <button className={mode === "yaml" ? "tab active" : "tab"} onClick={() => setMode("yaml")}>
              Raw YAML
            </button>
          </Explainable>
        </div>
        <div className="spacer" />
        <Explainable id="config.diff" as="span">
          <button className="btn" onClick={() => setShowDiff((v) => !v)}>
            {showDiff ? "Hide diff" : `Preview diff (${changedCount})`}
          </button>
        </Explainable>
        <Explainable id="config.apply" as="span">
          <button className="btn btn--primary" disabled={apply.isPending} onClick={() => apply.mutate()}>
            {apply.isPending ? "Applying…" : "Validate & apply"}
          </button>
        </Explainable>
      </div>

      {apply.isError && <p className="error">{(apply.error as Error).message}</p>}
      {apply.isSuccess && (
        <p className="notice">
          Written to disk. {apply.data?.restart_required ? "Restart SyncSage for server-level changes to take effect." : ""}
        </p>
      )}

      {showDiff && (
        <pre className="diff">
          {diff.map((line, i) => (
            <div key={i} className={`diff-line diff-line--${line.kind}`}>
              <span className="diff-gutter">{line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}</span>
              {line.text}
            </div>
          ))}
        </pre>
      )}

      {mode === "form" ? (
        <ObjectEditor value={working} onChange={setWorking} />
      ) : (
        <textarea
          className="text-input code yaml-editor"
          value={yamlText}
          onChange={(e) => setYamlText(e.target.value)}
          spellCheck={false}
        />
      )}
    </div>
  );
}
