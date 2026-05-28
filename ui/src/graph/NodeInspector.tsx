import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphLink, GraphNode } from "../api/types";
import { Explainable } from "../explain/Explainable";
import { colorForNode } from "./graphStyles";

interface NodeInspectorProps {
  selectedId: string | null;
  nodes: GraphNode[];
  links: GraphLink[];
  expandDepth: number;
  onDepthChange: (depth: number) => void;
  onExpand: (nodeId: string) => void;
  onPivot: (nodeId: string) => void;
}

type Tab = "overview" | "relationships" | "content";

const FILE_TYPES = new Set(["file", "document", "markdown_note", "chunk"]);

export function NodeInspector({
  selectedId,
  nodes,
  links,
  expandDepth,
  onDepthChange,
  onExpand,
  onPivot,
}: NodeInspectorProps) {
  const [tab, setTab] = useState<Tab>("overview");
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const node = selectedId ? nodeById.get(selectedId) : undefined;

  const explainQuery = useQuery({
    queryKey: ["explain", selectedId],
    queryFn: () => api.explain(selectedId as string),
    enabled: Boolean(selectedId),
  });

  const summaryQuery = useQuery({
    queryKey: ["summary", selectedId],
    queryFn: () =>
      api.fileSummary(String(node?.relative_path), node?.source_id ? String(node.source_id) : undefined),
    enabled: Boolean(selectedId && node?.relative_path && FILE_TYPES.has(node?.type ?? "")),
  });

  const relationships = useMemo(() => {
    if (!selectedId) return { outgoing: [], incoming: [] };
    const outgoing = links
      .filter((l) => l.source === selectedId)
      .map((l) => ({ other: l.target, type: l.type ?? "related" }));
    const incoming = links
      .filter((l) => l.target === selectedId)
      .map((l) => ({ other: l.source, type: l.type ?? "related" }));
    return { outgoing, incoming };
  }, [links, selectedId]);

  if (!selectedId || !node) {
    return (
      <div className="inspector inspector--empty">
        <p>Select a node to inspect its identity, relationships and content.</p>
        <p className="muted">Tip: turn on Explain mode in the top bar to learn the controls.</p>
      </div>
    );
  }

  return (
    <div className="inspector">
      <div className="inspector__title">
        <span className="dot" style={{ background: colorForNode(node.type) }} />
        <div>
          <div className="inspector__label">{node.label ?? node.id}</div>
          <div className="muted small">{node.type}</div>
        </div>
      </div>

      <div className="inspector__expand">
        <Explainable id="graph.depth" as="span">
          <label className="small muted">
            depth
            <input
              type="number"
              min={1}
              max={10}
              value={expandDepth}
              onChange={(e) => onDepthChange(Number(e.target.value))}
            />
          </label>
        </Explainable>
        <Explainable id="graph.expand" as="span">
          <button className="btn btn--primary" onClick={() => onExpand(selectedId)}>
            Expand sub-network
          </button>
        </Explainable>
      </div>

      <div className="tabs">
        <Explainable id="inspector.overview" as="span">
          <button className={tab === "overview" ? "tab active" : "tab"} onClick={() => setTab("overview")}>
            Overview
          </button>
        </Explainable>
        <Explainable id="inspector.relationships" as="span">
          <button
            className={tab === "relationships" ? "tab active" : "tab"}
            onClick={() => setTab("relationships")}
          >
            Relationships
          </button>
        </Explainable>
        <Explainable id="inspector.content" as="span">
          <button className={tab === "content" ? "tab active" : "tab"} onClick={() => setTab("content")}>
            Content
          </button>
        </Explainable>
      </div>

      {tab === "overview" && (
        <div className="kv">
          {Object.entries(node)
            .filter(([key]) => !["label", "type"].includes(key))
            .map(([key, value]) => (
              <div className="kv__row" key={key}>
                <span className="kv__key">{key}</span>
                <span className="kv__value">{renderValue(value)}</span>
              </div>
            ))}
          {explainQuery.data?.explanation && (
            <p className="explanation">{explainQuery.data.explanation}</p>
          )}
        </div>
      )}

      {tab === "relationships" && (
        <div className="relationships">
          <RelGroup
            title="Outgoing"
            items={relationships.outgoing}
            nodeById={nodeById}
            onPivot={onPivot}
          />
          <RelGroup
            title="Incoming"
            items={relationships.incoming}
            nodeById={nodeById}
            onPivot={onPivot}
          />
        </div>
      )}

      {tab === "content" && (
        <div className="content-tab">
          {node.summary ? (
            <pre className="content-block">{String(node.summary)}</pre>
          ) : summaryQuery.isLoading ? (
            <p className="muted">Loading content…</p>
          ) : summaryQuery.data?.summary ? (
            <pre className="content-block">{String(summaryQuery.data.summary)}</pre>
          ) : (
            <p className="muted">No indexed content for this node type.</p>
          )}
        </div>
      )}
    </div>
  );
}

function RelGroup({
  title,
  items,
  nodeById,
  onPivot,
}: {
  title: string;
  items: { other: string; type: string }[];
  nodeById: Map<string, GraphNode>;
  onPivot: (id: string) => void;
}) {
  if (items.length === 0) return <div className="rel-group muted small">{title}: none</div>;
  return (
    <div className="rel-group">
      <div className="rel-group__title">{title}</div>
      {items.map((item, index) => (
        <button
          key={`${item.type}-${item.other}-${index}`}
          className="rel-item"
          onClick={() => onPivot(item.other)}
          title={item.other}
        >
          <span className="rel-item__type">{item.type}</span>
          <span className="rel-item__node">{nodeById.get(item.other)?.label ?? item.other}</span>
        </button>
      ))}
    </div>
  );
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
