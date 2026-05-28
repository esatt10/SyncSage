import Markdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { useExplain } from "./ExplainContext";
import { referenceMarkdown } from "./referenceContent";

// Slide-over panel rendering the Markdown reference with LaTeX (KaTeX) math.
export function ReferencePanel() {
  const { panelOpen, setPanelOpen } = useExplain();
  if (!panelOpen) return null;
  return (
    <>
      <div className="panel-scrim" onClick={() => setPanelOpen(false)} />
      <aside className="reference-panel" aria-label="How SyncSage works">
        <header className="reference-panel__header">
          <span>How SyncSage works</span>
          <button className="btn btn--ghost" onClick={() => setPanelOpen(false)}>
            Close
          </button>
        </header>
        <div className="reference-panel__body markdown">
          <Markdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
            {referenceMarkdown}
          </Markdown>
        </div>
      </aside>
    </>
  );
}
