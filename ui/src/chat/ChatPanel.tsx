import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AssistantStatus, ChatAnswer } from "../api/types";
import { AnswerBody } from "./AnswerBody";
import { SourceStrip } from "./SourceStrip";

export interface ChatTurn {
  id: string;
  question: string;
  answer?: ChatAnswer;
  error?: string;
}

interface ChatPanelProps {
  status?: AssistantStatus;
  sessionId: string | null;
  /** Restrict retrieval to one source, or null for the whole knowledge base. */
  sourceFilter: string | null;
  /** Per-request workflow override, or null to use the configured default. */
  workflow: string | null;
  hasContent: boolean;
  onAnswer: (answer: ChatAnswer) => void;
  onCitationClick: (nodeId: string | undefined) => void;
  onConnectModel: () => void;
}

const SUGGESTIONS = [
  "What is this knowledge base about?",
  "Summarize the main themes across my sources.",
  "What decisions are recorded here, and why?",
];

export function ChatPanel({
  status,
  sessionId,
  sourceFilter,
  workflow,
  hasContent,
  onAnswer,
  onCitationClick,
  onConnectModel,
}: ChatPanelProps) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const ask = useMutation({
    mutationFn: (question: string) =>
      api.chat({
        question,
        session_id: sessionId,
        source_name: sourceFilter,
        workflow,
      }),
    onSuccess: (answer, question) => {
      setTurns((prev) =>
        prev.map((turn) =>
          turn.question === question && !turn.answer && !turn.error ? { ...turn, answer } : turn,
        ),
      );
      onAnswer(answer);
    },
    onError: (error: Error, question) => {
      setTurns((prev) =>
        prev.map((turn) =>
          turn.question === question && !turn.answer && !turn.error
            ? { ...turn, error: error.message }
            : turn,
        ),
      );
    },
  });

  // Keep the newest turn in view as answers stream in.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns, ask.isPending]);

  const submit = (text: string) => {
    const question = text.trim();
    if (!question || ask.isPending) return;
    setTurns((prev) => [...prev, { id: `${Date.now()}-${prev.length}`, question }]);
    setDraft("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    ask.mutate(question);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(draft);
    }
  };

  const extractive = status ? !status.ready : false;

  return (
    <div className="chat">
      <div className="chat__scroll" ref={scrollRef}>
        {turns.length === 0 ? (
          <div className="chat-empty">
            <h2>Ask your knowledge base</h2>
            <p>
              {hasContent
                ? "Answers are grounded in your indexed sources and cite the passages they came from."
                : "Nothing is indexed yet. Add a source and it becomes answerable here."}
            </p>
            {hasContent ? (
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    className="suggestion"
                    onClick={() => submit(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {turns.map((turn) => (
          <div key={turn.id}>
            <div className="msg msg--user">
              <div className="msg__bubble">{turn.question}</div>
            </div>
            {turn.error ? (
              <div className="msg">
                <div className="banner banner--error" style={{ marginBottom: 0 }}>
                  {turn.error}
                </div>
              </div>
            ) : turn.answer ? (
              <AnswerTurn answer={turn.answer} onCitationClick={onCitationClick} />
            ) : (
              <div className="msg">
                <span className="thinking">
                  <span className="spinner" />{" "}
                  {workflow === "simple"
                    ? "Searching your sources…"
                    : "Planning, searching and checking the evidence…"}
                </span>
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="chat__composer">
        <div className="composer">
          <textarea
            ref={textareaRef}
            rows={1}
            value={draft}
            placeholder={
              sourceFilter ? `Ask about ${sourceFilter}…` : "Ask anything about your sources…"
            }
            onChange={(event) => {
              setDraft(event.target.value);
              const el = event.target;
              el.style.height = "auto";
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
            }}
            onKeyDown={onKeyDown}
            aria-label="Ask a question"
          />
          <button
            className="composer__send"
            onClick={() => submit(draft)}
            disabled={!draft.trim() || ask.isPending}
            aria-label="Send"
            title="Send (Enter)"
          >
            ↑
          </button>
        </div>
        <div className="composer__hint">
          {sourceFilter ? <span className="pill pill--accent">scoped to {sourceFilter}</span> : null}
          {extractive ? (
            <>
              <span>No model connected — answers are extracted passages, not synthesis.</span>
              <button className="btn btn--small" onClick={onConnectModel}>
                Connect a model
              </button>
            </>
          ) : status?.credential_source === "session" ? (
            <span>
              Using {status.session?.provider} · key held for this session only
            </span>
          ) : status?.credential_source === "environment" ? (
            <span>Using {status.configured_provider} from the server environment</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * What the agent actually did.
 *
 * Collapsed by default — the answer is the product — but one click away,
 * because "it searched three times and walked the graph" is the difference
 * between trusting a slow answer and assuming it hung.
 */
function AgentTrace({ steps }: { steps: NonNullable<ChatAnswer["steps"]> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="trace">
      <button className="trace__toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "\u25be" : "\u25b8"} {steps.length} steps
      </button>
      {open ? (
        <ol className="trace__list">
          {steps.map((step, index) => (
            <li key={`${step.name}-${index}`}>
              <span className="trace__name">{step.name}</span>
              <span className="trace__detail">{step.detail}</span>
              {step.passages > 0 ? <span className="trace__count">{step.passages}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

function AnswerTurn({
  answer,
  onCitationClick,
}: {
  answer: ChatAnswer;
  onCitationClick: (nodeId: string | undefined) => void;
}) {
  const byIndex = new Map(answer.citations.map((c) => [c.index, c]));
  return (
    <div className="msg">
      <div className="msg__answer">
        <AnswerBody
          text={answer.answer}
          onCite={(index) => onCitationClick(byIndex.get(index)?.node_id)}
        />
      </div>
      {answer.citations.length > 0 ? (
        <SourceStrip citations={answer.citations} onSelect={onCitationClick} />
      ) : null}
      {answer.steps && answer.steps.length > 1 ? <AgentTrace steps={answer.steps} /> : null}
      <div className="msg__meta">
        {answer.workflow ? <span className="pill">{answer.workflow}</span> : null}
        {answer.mode === "llm" ? (
          <span>
            {answer.provider}
            {answer.model ? ` · ${answer.model}` : ""}
          </span>
        ) : (
          <span>extracted passages</span>
        )}
        <span>·</span>
        <span>{answer.citations.length} sources</span>
        {answer.facts.length > 0 ? (
          <>
            <span>·</span>
            <span>{answer.facts.length} graph facts</span>
          </>
        ) : null}
        {answer.error ? <span className="error">· {answer.error}</span> : null}
      </div>
    </div>
  );
}
