/**
 * Reviewing what the region proposes, at the scale it actually proposes it.
 *
 * A list of one-line proposals is fine for three and useless for three
 * hundred, and it never answered the first question a reviewer has: *why is
 * this being suggested?* `router -> pheasant-flock` with "seen 4×" attached is
 * an assertion, not evidence.
 *
 * So the page is built in three layers, because a reviewer asks three
 * questions in order:
 *
 *   1. **What is claimed** — the row: rule, kind, scope, how strong.
 *   2. **On what basis** — expand once: the calls behind it. What was asked,
 *      what came back.
 *   3. **How do I check it** — expand again: the spans. Which trace, which
 *      parent, when, how long — enough to follow one proposal back through a
 *      collector to the request that produced it.
 *
 * Nothing below layer 1 is fetched until it is opened. A hundred proposals
 * would otherwise be a hundred evidence queries nobody asked for.
 *
 * And at scale the work is triage, not reading: proposals group by rule
 * (a hundred candidates are usually three or four rules), filter by text,
 * and act in bulk. Selecting a whole group and promoting it is one gesture.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { MemoryCandidate } from "../api/types";

/** Rows rendered per group before "show more". The DOM cost of a few hundred
 *  expanded-capable rows is real, and a reviewer works top-down anyway. */
const PAGE = 25;

function relative(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function parseList(raw: string | null | undefined): string[] {
  if (!raw) return [];
  try {
    const value = JSON.parse(raw);
    return Array.isArray(value) ? value.map(String) : [];
  } catch {
    return [];
  }
}

/** Layers 2 and 3 for one proposal. Fetched only when opened. */
function Evidence({ candidate }: { candidate: MemoryCandidate }) {
  const [showTrace, setShowTrace] = useState(false);
  const trail = useQuery({
    queryKey: ["memory-candidate-evidence", candidate.id],
    queryFn: () => api.memoryCandidateEvidence(candidate.id),
    retry: false,
  });

  if (trail.isPending) return <p className="candidate__hint">Loading the evidence…</p>;
  if (trail.isError) {
    return <p className="error">Could not load the evidence: {String(trail.error)}</p>;
  }

  const { interactions, named, found } = trail.data;
  return (
    <div className="candidate__evidence">
      <div className="candidate__evidence-head">
        <strong>Why this was proposed</strong>
        {/* Evidence ages out: the hot window is retention-bounded, so a
            pending proposal can outlive the rows behind it. Saying so beats
            showing a short list that looks like the whole story. */}
        {found < named && (
          <span className="candidate__hint">
            {found} of {named} recorded calls still in the hot window — the rest
            have aged out past the retention window.
          </span>
        )}
      </div>

      {interactions.length === 0 ? (
        <p className="candidate__hint">
          No contributing calls are still retained. The counts above remain the
          totals the rule actually saw.
        </p>
      ) : (
        <table className="candidate__calls">
          <thead>
            <tr>
              <th>Asked</th>
              <th>Came back</th>
              <th>Where</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {interactions.map((call) => {
              const paths = parseList(call.result_paths_json);
              return (
                <tr key={call.id}>
                  <td className="candidate__q">{call.query_text ?? <em>redacted</em>}</td>
                  <td>
                    {paths.length === 0 ? (
                      <em className="candidate__nothing">nothing</em>
                    ) : (
                      paths.slice(0, 3).map((path) => (
                        <code key={path} className="candidate__path">
                          {path}
                        </code>
                      ))
                    )}
                    {paths.length > 3 && (
                      <span className="candidate__hint"> +{paths.length - 3}</span>
                    )}
                  </td>
                  <td className="candidate__hint">
                    {call.modality} · {call.session_id ?? "—"}
                  </td>
                  <td className="candidate__hint">{relative(call.started_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {interactions.length > 0 && (
        <>
          <button
            type="button"
            className="candidate__disclose"
            onClick={() => setShowTrace((open) => !open)}
            aria-expanded={showTrace}
          >
            {showTrace ? "▾" : "▸"} Trace — the steps that led here
          </button>
          {showTrace && (
            <ol className="candidate__trace">
              {interactions.map((call) => (
                <li key={call.id}>
                  <span className={`candidate__status candidate__status--${call.status}`}>
                    {call.status}
                  </span>
                  <code>{call.operation}</code>
                  <span className="candidate__hint">
                    {call.duration_ms === null ? "—" : `${call.duration_ms.toFixed(1)}ms`}
                  </span>
                  <span className="candidate__hint">trace</span>
                  <code className="candidate__span" title="trace id">
                    {call.trace_id.slice(0, 12)}
                  </code>
                  <span className="candidate__hint">span</span>
                  <code className="candidate__span" title="span id">
                    {call.span_id.slice(0, 8)}
                  </code>
                  {call.parent_span_id && (
                    <>
                      <span className="candidate__hint">← parent</span>
                      <code className="candidate__span" title="parent span id">
                        {call.parent_span_id.slice(0, 8)}
                      </code>
                    </>
                  )}
                </li>
              ))}
              <li className="candidate__trace-end">
                <span className="candidate__status candidate__status--formed">consolidated</span>
                <span className="candidate__hint">
                  {candidate.rule_id} proposed “{candidate.text}” from the{" "}
                  {candidate.observations} call{candidate.observations === 1 ? "" : "s"} above,
                  across {candidate.sessions} session
                  {candidate.sessions === 1 ? "" : "s"}.
                </span>
              </li>
            </ol>
          )}
        </>
      )}
    </div>
  );
}

export function CandidateReview() {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [shown, setShown] = useState<Record<string, number>>({});

  const listing = useQuery({
    queryKey: ["memory-candidates"],
    queryFn: () => api.memoryCandidates({ status: "pending" }),
    retry: false,
  });

  const decide = useMutation({
    mutationFn: async ({ ids, promote }: { ids: string[]; promote: boolean }) => {
      // Sequential, not parallel: each promotion writes a memory record
      // through the ordinary path, and a burst of concurrent writes into one
      // memory source buys nothing on a review action a human just clicked.
      for (const id of ids) {
        if (promote) await api.memoryCandidatePromote(id);
        else await api.memoryCandidateReject(id);
      }
      return ids.length;
    },
    onSuccess: () => {
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ["memory-candidates"] });
      queryClient.invalidateQueries({ queryKey: ["memory"] });
    },
  });

  const candidates = listing.data?.candidates ?? [];

  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const matching = needle
      ? candidates.filter(
          (c) =>
            c.text.toLowerCase().includes(needle) ||
            c.rule_id.toLowerCase().includes(needle) ||
            (c.subject ?? "").toLowerCase().includes(needle),
        )
      : candidates;
    const byRule = new Map<string, MemoryCandidate[]>();
    for (const candidate of matching) {
      const bucket = byRule.get(candidate.rule_id) ?? [];
      // Strongest evidence first: a reviewer working top-down should meet the
      // best-supported proposals before the marginal ones.
      bucket.push(candidate);
      byRule.set(candidate.rule_id, bucket);
    }
    for (const bucket of byRule.values()) {
      bucket.sort(
        (a, b) => b.sessions - a.sessions || b.observations - a.observations ||
          a.text.localeCompare(b.text),
      );
    }
    return [...byRule.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [candidates, filter]);

  if (listing.isError || candidates.length === 0) return null;

  const visible = groups.flatMap(([, bucket]) => bucket).map((c) => c.id);
  const allVisibleSelected =
    visible.length > 0 && visible.every((id) => selected.has(id));

  const toggle = (set: Set<string>, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  };

  return (
    <section className="section candidates">
      <h2>
        Proposed <span className="candidates__count">{candidates.length}</span>
      </h2>
      <p className="section__hint">
        Patterns this region noticed in how it is used. <strong>These are not
        memories yet</strong> — nothing here is retrievable until you promote
        it. Open one to see the calls it came from. Rejecting is permanent: it
        will not be proposed again.
      </p>

      <div className="candidates__toolbar">
        <input
          className="input"
          type="search"
          placeholder="Filter proposals…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          aria-label="Filter proposals"
        />
        <label className="candidates__all">
          <input
            type="checkbox"
            checked={allVisibleSelected}
            onChange={() =>
              setSelected(allVisibleSelected ? new Set() : new Set(visible))
            }
          />
          Select all shown
        </label>
        <span className="candidates__selection">
          {selected.size > 0 ? `${selected.size} selected` : ""}
        </span>
        <button
          type="button"
          className="button button--primary"
          disabled={selected.size === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: [...selected], promote: true })}
        >
          Promote
        </button>
        <button
          type="button"
          className="button"
          disabled={selected.size === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: [...selected], promote: false })}
        >
          Reject
        </button>
      </div>
      {decide.isError && <p className="error">{String(decide.error)}</p>}

      {groups.map(([rule, bucket]) => {
        const isCollapsed = collapsed.has(rule);
        const limit = shown[rule] ?? PAGE;
        const groupIds = bucket.map((c) => c.id);
        const groupSelected = groupIds.every((id) => selected.has(id));
        return (
          <div className="candidates__group" key={rule}>
            <div className="candidates__group-head">
              <button
                type="button"
                className="candidate__disclose"
                onClick={() => setCollapsed(toggle(collapsed, rule))}
                aria-expanded={!isCollapsed}
              >
                {isCollapsed ? "▸" : "▾"} <code>{rule}</code>
                <span className="candidates__count">{bucket.length}</span>
              </button>
              <button
                type="button"
                className="candidates__link"
                onClick={() =>
                  setSelected((current) => {
                    const next = new Set(current);
                    groupIds.forEach((id) => (groupSelected ? next.delete(id) : next.add(id)));
                    return next;
                  })
                }
              >
                {groupSelected ? "clear group" : "select group"}
              </button>
            </div>

            {!isCollapsed &&
              bucket.slice(0, limit).map((candidate) => {
                const isOpen = opened.has(candidate.id);
                return (
                  <div className="candidate" key={candidate.id}>
                    <div className="candidate__row">
                      <input
                        type="checkbox"
                        checked={selected.has(candidate.id)}
                        onChange={() => setSelected(toggle(selected, candidate.id))}
                        aria-label={`Select ${candidate.text}`}
                      />
                      <button
                        type="button"
                        className="candidate__disclose"
                        onClick={() => setOpened(toggle(opened, candidate.id))}
                        aria-expanded={isOpen}
                        aria-label={isOpen ? "Hide evidence" : "Show evidence"}
                      >
                        {isOpen ? "▾" : "▸"}
                      </button>
                      <span className="candidate__text">{candidate.text}</span>
                      <span className="chip">{candidate.kind}</span>
                      <span className="chip">{candidate.scope}</span>
                      <span className="candidate__strength" title="how strong the evidence is">
                        {candidate.observations}× / {candidate.sessions} session
                        {candidate.sessions === 1 ? "" : "s"}
                      </span>
                      <span className="candidate__hint">{relative(candidate.last_seen)}</span>
                      <span className="candidate__row-actions">
                        <button
                          type="button"
                          className="button button--primary"
                          disabled={decide.isPending}
                          onClick={() =>
                            decide.mutate({ ids: [candidate.id], promote: true })
                          }
                        >
                          Promote
                        </button>
                        <button
                          type="button"
                          className="button"
                          disabled={decide.isPending}
                          onClick={() =>
                            decide.mutate({ ids: [candidate.id], promote: false })
                          }
                        >
                          Reject
                        </button>
                      </span>
                    </div>
                    {isOpen && <Evidence candidate={candidate} />}
                  </div>
                );
              })}

            {!isCollapsed && bucket.length > limit && (
              <button
                type="button"
                className="candidates__link"
                onClick={() => setShown({ ...shown, [rule]: limit + PAGE })}
              >
                Show {Math.min(PAGE, bucket.length - limit)} more of {bucket.length}
              </button>
            )}
          </div>
        );
      })}
    </section>
  );
}
