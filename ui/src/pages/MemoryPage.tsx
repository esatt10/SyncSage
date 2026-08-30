import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { CandidateReview } from "../memory/CandidateReview";
import type { MemoryRecord, MemoryScope } from "../api/types";

const SCOPES: MemoryScope[] = ["session", "user", "org"];

/**
 * What the region remembers, and the ability to correct it.
 *
 * The `/memory` endpoints have existed since Step 33.1 and nothing in the UI
 * had ever called them: memory could be written by an agent and read back by
 * an agent, but a person had no way to see what was being asserted on their
 * behalf — or to fix it. That is the gap this page closes.
 *
 * Corrections are made by *superseding*, never by editing: records are
 * append-only files, and the supersedes chain is what `as_of` reads to answer
 * "what did we believe then". An edit-in-place button would quietly destroy
 * that history.
 */
export function MemoryPage() {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<MemoryScope | "all">("all");
  const [currentOnly, setCurrentOnly] = useState(true);
  const [draft, setDraft] = useState("");
  const [draftScope, setDraftScope] = useState<MemoryScope>("user");
  const [draftSubject, setDraftSubject] = useState("");
  const [supersedes, setSupersedes] = useState<string | null>(null);

  const listing = useQuery({
    queryKey: ["memory", scope, currentOnly],
    queryFn: () =>
      api.memory({
        ...(scope === "all" ? {} : { scope }),
        current_only: currentOnly,
      }),
    retry: false,
  });

  const write = useMutation({
    mutationFn: () =>
      api.memoryWrite({
        text: draft.trim(),
        scope: draftScope,
        subject: draftSubject.trim() || null,
        supersedes,
      }),
    onSuccess: () => {
      setDraft("");
      setDraftSubject("");
      setSupersedes(null);
      queryClient.invalidateQueries({ queryKey: ["memory"] });
    },
  });

  const enable = useMutation({
    mutationFn: () => api.memoryEnable(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  const consolidate = useMutation({
    mutationFn: () => api.memoryConsolidate(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory"] });
      queryClient.invalidateQueries({ queryKey: ["memory-candidates"] });
    },
  });


  const records = listing.data?.records ?? [];
  const bySubject = useMemo(() => {
    const groups = new Map<string, MemoryRecord[]>();
    for (const record of records) {
      const key = record.subject?.trim() || "(no subject)";
      const bucket = groups.get(key) ?? [];
      bucket.push(record);
      groups.set(key, bucket);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [records]);

  // A memory source is optional, so "not configured" is an ordinary state
  // rather than an error — the API answers 400 for it, and telling someone to
  // check the logs would be the wrong reaction to a feature they never enabled.
  const notConfigured = listing.isError;

  return (
    <div className="page memory-page">
      <header className="page__header">
        <div>
          <h1>Memory</h1>
          <p className="page__subtitle">
            What agents have recorded in this knowledge base. Recall is ordinary
            search — these are the assertions behind it.
          </p>
        </div>
        <button
          className="button"
          onClick={() => consolidate.mutate()}
          disabled={consolidate.isPending || notConfigured}
          title="Archive superseded and expired records, then re-index"
        >
          {consolidate.isPending ? "Consolidating…" : "Consolidate"}
        </button>
      </header>

      {notConfigured ? (
        <div className="card memory-empty">
          <h2>Memory is not enabled</h2>
          <p>
            Agents can record what they learn as ordinary Markdown files, indexed
            by the same pipeline as everything else — recall is just search.
            Enabling creates a <code>memory/</code> folder and registers it.
          </p>
          <button
            className="button button--primary"
            onClick={() => enable.mutate()}
            disabled={enable.isPending}
          >
            {enable.isPending ? "Enabling…" : "Enable memory"}
          </button>
          {enable.isError && <p className="error">{String(enable.error)}</p>}
        </div>
      ) : (
        <>
          <CandidateReview />

          <section className="section">
            <h2>Write a memory</h2>
            <p className="section__hint">
              {supersedes
                ? `This will supersede ${supersedes} — the original is kept and stops being current.`
                : "Recorded as an assertion an agent can retrieve later."}
            </p>
            <textarea
              className="input memory-draft"
              rows={3}
              value={draft}
              placeholder="The staging cluster lives in us-east-2."
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="memory-draft__row">
              <select
                className="input"
                value={draftScope}
                onChange={(event) => setDraftScope(event.target.value as MemoryScope)}
              >
                {SCOPES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
              <input
                className="input"
                placeholder="subject (optional)"
                value={draftSubject}
                onChange={(event) => setDraftSubject(event.target.value)}
              />
              <button
                className="button button--primary"
                disabled={!draft.trim() || write.isPending}
                onClick={() => write.mutate()}
              >
                {write.isPending ? "Saving…" : supersedes ? "Correct" : "Remember"}
              </button>
              {supersedes && (
                <button className="button" onClick={() => setSupersedes(null)}>
                  Cancel correction
                </button>
              )}
            </div>
            {write.isError && <p className="error">{String(write.error)}</p>}
          </section>

          <section className="section">
            <div className="memory-filters">
              <select
                className="input"
                value={scope}
                onChange={(event) => setScope(event.target.value as MemoryScope | "all")}
              >
                <option value="all">every scope</option>
                {SCOPES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={currentOnly}
                  onChange={(event) => setCurrentOnly(event.target.checked)}
                />
                current only
              </label>
              <span className="section__hint">
                {records.length} record{records.length === 1 ? "" : "s"}
                {currentOnly ? " still current" : " including corrected ones"}
              </span>
            </div>

            {records.length === 0 ? (
              <div className="card memory-empty">
                <p>
                  Nothing remembered yet. Agents write here with the{" "}
                  <code>memory_write</code> tool, or use the box above.
                </p>
              </div>
            ) : (
              bySubject.map(([subject, group]) => (
                <div key={subject} className="memory-group">
                  <h3>{subject}</h3>
                  {group.map((record) => (
                    <MemoryCard
                      key={record.record_id}
                      record={record}
                      onCorrect={() => {
                        setSupersedes(record.record_id);
                        setDraftScope(record.scope);
                        setDraftSubject(record.subject ?? "");
                      }}
                    />
                  ))}
                </div>
              ))
            )}
          </section>
        </>
      )}
    </div>
  );
}

function MemoryCard({
  record,
  onCorrect,
}: {
  record: MemoryRecord;
  onCorrect: () => void;
}) {
  const superseded = Boolean(record.valid_until);
  return (
    <article className={`memory-card${superseded ? " memory-card--superseded" : ""}`}>
      <div className="memory-card__meta">
        <span className={`badge badge--${record.scope}`}>{record.scope}</span>
        {record.kind !== "fact" && <span className="badge">{record.kind}</span>}
        <time dateTime={record.asserted_at}>{record.asserted_at}</time>
        {record.written_by && <span className="memory-card__by">{record.written_by}</span>}
        {superseded && <span className="badge badge--stale">corrected</span>}
      </div>
      <p className="memory-card__text">{record.text}</p>
      <div className="memory-card__actions">
        {record.supersedes && (
          <span className="memory-card__chain">replaces {record.supersedes}</span>
        )}
        {!superseded && (
          <button className="button button--small" onClick={onCorrect}>
            Correct
          </button>
        )}
      </div>
    </article>
  );
}
