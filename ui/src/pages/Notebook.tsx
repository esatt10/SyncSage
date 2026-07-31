import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ChatAnswer, GraphFact, GraphLink, GraphNode } from "../api/types";
import { ChatPanel } from "../chat/ChatPanel";
import { ModelDialog } from "../chat/ModelDialog";
import { GraphCanvas } from "../graph/GraphCanvas";
import { NodeInspector } from "../graph/NodeInspector";
import { GraphLegend } from "../graph/GraphLegend";
import { SourceRail } from "../components/SourceRail";
import { EmptyState } from "../components/EmptyState";
import { WorkflowPanel } from "../components/WorkflowPanel";
import { NOISY_NODE_TYPES, colorForNode } from "../graph/graphStyles";
import { useSessionId } from "../hooks/useSessionId";

type PanelTab = "graph" | "facts" | "node";

/**
 * The workspace: sources on the left, conversation in the middle, knowledge on
 * the right. Asking a question drives the graph — cited nodes get outlined and
 * focused — so the answer and the structure behind it stay side by side.
 */
export function Notebook() {
  const [sessionId, setSessionId] = useSessionId();
  const [hiddenTypes, setHiddenTypes] = useState<string[]>(NOISY_NODE_TYPES);
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusIds, setFocusIds] = useState<string[]>([]);
  const [extra, setExtra] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({
    nodes: [],
    links: [],
  });
  const [answer, setAnswer] = useState<ChatAnswer | null>(null);
  const [panelTab, setPanelTab] = useState<PanelTab>("graph");
  const [showModelDialog, setShowModelDialog] = useState(false);
  const [workflow, setWorkflow] = useState<string | null>(null);
  const [showWorkflowDialog, setShowWorkflowDialog] = useState(false);
  const [expanding, setExpanding] = useState(false);

  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });
  const status = useQuery({
    queryKey: ["assistant-status", sessionId],
    queryFn: () => api.assistantStatus(sessionId),
  });
  const graphQuery = useQuery({
    queryKey: ["graph", hiddenTypes, sourceFilter],
    queryFn: () =>
      api.graph({
        excludeTypes: hiddenTypes,
        source: sourceFilter ?? undefined,
      }),
  });

  const base = graphQuery.data ?? { nodes: [], links: [] };
  const merged = useMemo(() => {
    const nodeMap = new Map<string, GraphNode>();
    [...base.nodes, ...extra.nodes].forEach((node) => node.id && nodeMap.set(node.id, node));
    const linkKey = (link: GraphLink) =>
      `${link.source}|${link.type}|${link.key ?? 0}|${link.target}`;
    const linkMap = new Map<string, GraphLink>();
    [...base.links, ...extra.links].forEach((link) => linkMap.set(linkKey(link), link));
    return { nodes: [...nodeMap.values()], links: [...linkMap.values()] };
  }, [base, extra]);

  const nodeById = useMemo(
    () => new Map(merged.nodes.map((node) => [node.id, node])),
    [merged.nodes],
  );

  const citedIds = useMemo(
    () => (answer?.citations ?? []).filter((c) => c.node_id).map((c) => c.node_id as string),
    [answer],
  );

  /**
   * Focus a node that may not be in the current view — a citation can point at
   * a chunk while chunks are hidden, or at a node beyond the preview limit.
   * Pull its neighbourhood in rather than silently doing nothing.
   */
  const focusNode = useCallback(
    async (nodeId: string | undefined) => {
      if (!nodeId) return;
      setPanelTab("node");
      if (nodeById.has(nodeId)) {
        setSelectedId(nodeId);
        setFocusIds([nodeId]);
        return;
      }
      setExpanding(true);
      try {
        const slice = await api.graphSlice(nodeId, 1);
        if (slice.nodes.length > 0) {
          setExtra((prev) => ({
            nodes: [...prev.nodes, ...slice.nodes],
            links: [...prev.links, ...slice.links],
          }));
          setSelectedId(nodeId);
          setFocusIds([nodeId, ...slice.nodes.map((n) => n.id)]);
        }
      } finally {
        setExpanding(false);
      }
    },
    [nodeById],
  );

  const expand = useCallback(async (nodeId: string, depth = 1) => {
    setExpanding(true);
    try {
      const slice = await api.graphSlice(nodeId, depth);
      setExtra((prev) => ({
        nodes: [...prev.nodes, ...slice.nodes],
        links: [...prev.links, ...slice.links],
      }));
      setFocusIds([nodeId, ...slice.nodes.map((n) => n.id)]);
    } finally {
      setExpanding(false);
    }
  }, []);

  const onAnswer = useCallback((next: ChatAnswer) => {
    setAnswer(next);
    setPanelTab(next.facts.length > 0 ? "facts" : "graph");
    // Light up the passages behind the answer on the canvas.
    setFocusIds(next.focus_node_ids);
  }, []);

  const toggleType = (type: string) =>
    setHiddenTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type],
    );

  const hasContent = overview.data?.has_content ?? false;

  if (overview.isSuccess && !hasContent) {
    return (
      <EmptyState
        overview={overview.data}
        onAdded={() => {
          overview.refetch();
          graphQuery.refetch();
        }}
      />
    );
  }

  return (
    <div className="notebook">
      <aside className="pane pane--rail">
        <SourceRail
          sources={overview.data?.sources ?? []}
          selected={sourceFilter}
          onSelect={setSourceFilter}
          onChanged={() => {
            overview.refetch();
            graphQuery.refetch();
          }}
        />
      </aside>

      <section className="pane">
        <header className="pane__header">
          <span className="pane__title">Chat</span>
          <div className="pane__actions">
            <button
              className="btn btn--small"
              onClick={() => setShowWorkflowDialog(true)}
              title="Choose how questions are answered"
            >
              {workflow ?? "Workflow"}
            </button>
            <button
              className="btn btn--small"
              onClick={() => setShowModelDialog(true)}
              title="Connect an LLM to synthesize answers"
            >
              {status.data?.ready ? "Model connected" : "Connect model"}
            </button>
          </div>
        </header>
        <div className="pane__body" style={{ overflow: "hidden" }}>
          <ChatPanel
            status={status.data}
            sessionId={sessionId}
            sourceFilter={sourceFilter}
            workflow={workflow}
            hasContent={hasContent}
            onAnswer={onAnswer}
            onCitationClick={focusNode}
            onConnectModel={() => setShowModelDialog(true)}
          />
        </div>
      </section>

      <aside className="pane pane--panel">
        <header className="pane__header">
          <div className="tabs" style={{ flex: 1 }}>
            <button
              className={panelTab === "graph" ? "tab active" : "tab"}
              onClick={() => setPanelTab("graph")}
            >
              Graph
            </button>
            <button
              className={panelTab === "facts" ? "tab active" : "tab"}
              onClick={() => setPanelTab("facts")}
            >
              Facts{answer?.facts.length ? ` (${answer.facts.length})` : ""}
            </button>
            <button
              className={panelTab === "node" ? "tab active" : "tab"}
              onClick={() => setPanelTab("node")}
            >
              Node
            </button>
          </div>
        </header>

        {panelTab === "graph" ? (
          <>
            <div className="graph-pane__body">
              <div className="graph-overlay">
                <span className="graph-badge">
                  {merged.nodes.length}
                  {graphQuery.data?.matched_nodes &&
                  graphQuery.data.matched_nodes > merged.nodes.length
                    ? ` of ${graphQuery.data.matched_nodes}`
                    : ""}{" "}
                  nodes
                </span>
                {citedIds.length > 0 ? (
                  <span className="graph-badge">{citedIds.length} cited</span>
                ) : null}
                {expanding ? <span className="graph-badge">loading…</span> : null}
              </div>
              {graphQuery.isLoading ? (
                <div className="centered muted">
                  <span className="spinner" />
                </div>
              ) : merged.nodes.length === 0 ? (
                <div className="centered muted small">
                  Nothing to show with the current filters.
                </div>
              ) : (
                <GraphCanvas
                  nodes={merged.nodes}
                  links={merged.links}
                  selectedId={selectedId}
                  focusIds={focusIds}
                  citedIds={citedIds}
                  layoutName="auto"
                  spacing={1}
                  shapeAlgorithm="node_type"
                  onSelect={(id) => {
                    setSelectedId(id);
                    setPanelTab("node");
                  }}
                />
              )}
            </div>
            <GraphLegend
              counts={overview.data?.node_counts ?? {}}
              hidden={hiddenTypes}
              onToggle={toggleType}
            />
          </>
        ) : null}

        {panelTab === "facts" ? (
          <div className="pane__body pane__body--pad">
            <FactList facts={answer?.facts ?? []} onSelect={focusNode} />
          </div>
        ) : null}

        {panelTab === "node" ? (
          <div className="pane__body">
            <NodeInspector
              selectedId={selectedId}
              nodes={merged.nodes}
              links={merged.links}
              onExpand={expand}
              onPivot={focusNode}
            />
          </div>
        ) : null}
      </aside>

      {showWorkflowDialog ? (
        <div className="modal-scrim" onClick={() => setShowWorkflowDialog(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <header className="modal__header">
              <h2>How questions are answered</h2>
              <button
                className="btn btn--ghost btn--icon"
                onClick={() => setShowWorkflowDialog(false)}
                aria-label="Close"
              >
                \u2715
              </button>
            </header>
            <WorkflowPanel selected={workflow} onSelect={setWorkflow} />
            <div className="modal__footer">
              <button className="btn btn--primary" onClick={() => setShowWorkflowDialog(false)}>
                Done
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {showModelDialog ? (
        <ModelDialog
          status={status.data}
          sessionId={sessionId}
          onSession={(id) => {
            setSessionId(id);
            status.refetch();
          }}
          onClose={() => setShowModelDialog(false)}
        />
      ) : null}
    </div>
  );
}

/**
 * Surfaced facts: subject–predicate–object triples read straight off the graph
 * around the passages an answer used. Both endpoints are clickable, so a fact
 * is a navigation handle rather than a static readout.
 */
function FactList({
  facts,
  onSelect,
}: {
  facts: GraphFact[];
  onSelect: (nodeId: string) => void;
}) {
  if (facts.length === 0) {
    return (
      <p className="muted small">
        Ask a question and the relationships SyncSage found around the cited passages
        show up here.
      </p>
    );
  }
  return (
    <div className="fact-list">
      {facts.map((fact, index) => (
        <div
          className="fact"
          key={`${fact.subject_id}-${fact.edge_type}-${fact.object_id}-${index}`}
        >
          <span
            className="source-dot"
            style={{ background: colorForNode(fact.object_type), borderRadius: "50%" }}
          />
          <button
            className="fact__object"
            onClick={() => onSelect(fact.object_id)}
            title={`Show ${fact.object} on the graph`}
          >
            {fact.object}
          </button>
          <span className="fact__from">
            <span className="fact__predicate">{fact.predicate_passive ?? fact.predicate}</span>
            <button
              className="fact__subject"
              onClick={() => onSelect(fact.subject_id)}
              title={fact.subject}
            >
              {fact.subject}
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}
