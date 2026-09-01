import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { TuningGate, TuningHistogram, TuningTrial } from "../api/types";

/** Stages a ranking parameter can actually move. Mirrors `tuning.stages`. */
const ACTIONABLE = new Set([
  "lexical_candidates",
  "vector_candidates",
  "filters",
  "fusion",
  "truncation",
]);

/** What each stage means, in the words a reader needs rather than the code's. */
const STAGE_HELP: Record<string, string> = {
  absent_from_corpus: "Not indexed. No ranking parameter can reach this.",
  candidates_missing: "No arm returned it, and the corpus was not checked.",
  lexical_candidates: "The text arm did not return it within its fetch window.",
  vector_candidates: "The vector arm did not return it.",
  graph_candidates: "The graph arm did not return it.",
  filters: "An arm had it and a filter removed it — ACL, memory policy, or section.",
  fusion: "It survived every filter and the merge ranked it below the cut.",
  truncation: "Fused above the cut, then deduplicated away.",
  served: "Returned. Nothing failed.",
};

const TERMINAL = new Set(["completed", "failed", "interrupted", "none"]);

/**
 * Which step of retrieval is failing, and what to do about it.
 *
 * The page is organised around one claim: an absent result has six possible
 * causes, they look identical after the merge, and they call for different
 * responses. So the histogram is the headline — not a score — and every stage
 * on it is labelled with whether a parameter can reach it at all. A region
 * whose misses are mostly `absent_from_corpus` should leave this page and go
 * look at indexing, and the page says so rather than offering a tuning button
 * that cannot help.
 *
 * Progress comes from `/tuning/status`, which reads `/state` rather than the
 * process running the batch — so this works when the browser is talking to a
 * replica that did not start it, and after the container that did was
 * restarted.
 */
export function TuningPage() {
  const queryClient = useQueryClient();
  const [showAllTrials, setShowAllTrials] = useState(false);

  const status = useQuery({
    queryKey: ["tuning", "status"],
    queryFn: () => api.tuningStatus(),
    // Polled only while something is actually running: a page left open on a
    // finished batch should not keep a database read going forever.
    refetchInterval: (query) =>
      TERMINAL.has(query.state.data?.status ?? "none") ? false : 2000,
  });
  const running = status.data?.status === "running";

  const report = useQuery({
    queryKey: ["tuning", "report", status.data?.experiment_id, status.data?.status],
    queryFn: () => api.tuningReport(),
    // 404 until a batch has finished, which is a normal state and not an error
    // worth retrying at.
    retry: false,
    enabled: !running,
  });
  const parameters = useQuery({
    queryKey: ["tuning", "parameters"],
    queryFn: () => api.tuningParameters(),
  });
  const bundles = useQuery({
    queryKey: ["tuning", "bundles"],
    queryFn: () => api.tuningBundles(),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["tuning"] });
  };
  const start = useMutation({
    mutationFn: (body: { diagnose_only?: boolean }) =>
      api.tuningRun({ force: true, ...body }),
    onSuccess: invalidate,
  });
  const apply = useMutation({
    mutationFn: (bundleId: string) => api.tuningApply(bundleId),
    onSuccess: invalidate,
  });
  const rollback = useMutation({
    mutationFn: () => api.tuningRollback(),
    onSuccess: invalidate,
  });

  const active = parameters.data?.active;
  const decision = report.data?.decision;
  const histogram = report.data?.diagnosis?.histogram;

  return (
    <div className="page tune-page">
      <header className="page__header">
        <div>
          <h1>Retrieval tuning</h1>
          <p className="page__lede">
            Which step of retrieval is losing documents, and whether a parameter can
            reach it. Producing a configuration bundle changes nothing; applying one
            re-ranks every replica.
          </p>
        </div>
        <div className="page__actions">
          <button
            type="button"
            className="btn"
            onClick={() => start.mutate({ diagnose_only: true })}
            disabled={running || start.isPending}
          >
            Diagnose only
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => start.mutate({})}
            disabled={running || start.isPending}
          >
            {running ? "Running…" : "Run a tuning batch"}
          </button>
        </div>
      </header>

      {status.data && status.data.status !== "none" ? (
        <section className="tune-section">
          <h2>Batch</h2>
          <div className="tuning__status">
            <span className={`badge badge--${status.data.status}`}>{status.data.status}</span>
            <span>{status.data.phase}</span>
            <span className="muted">{status.data.phase_detail}</span>
          </div>
          {status.data.total_units > 0 ? (
            <div className="eval-progress__bar">
              <div
                className="eval-progress__fill"
                style={{ width: `${Math.round((status.data.progress ?? 0) * 100)}%` }}
              />
              <span className="muted small">
                {status.data.completed_units}/{status.data.total_units} points ·{" "}
                {status.data.searches} searches
              </span>
            </div>
          ) : null}
          {status.data.attempts > 1 ? (
            <p className="muted">
              Attempt {status.data.attempts}: an earlier one was interrupted and this
              batch resumed from its stored trials.
            </p>
          ) : null}
          {status.data.error ? <p className="muted small tune-error">{status.data.error}</p> : null}
        </section>
      ) : null}

      <section className="tune-section">
        <h2>What this region ranks with</h2>
        {active ? (
          <>
            <p>
              Parameters come from <strong>{active.provenance}</strong>
              {active.bundle_id ? (
                <>
                  {" "}
                  — bundle <code>{active.bundle_id}</code>, applied{" "}
                  {active.bundle?.applied_at} by {active.bundle?.applied_by}
                </>
              ) : (
                " — the values in pheasant.yaml"
              )}
              .
            </p>
            {active.provenance === "bundle" ? (
              <button
                type="button"
                className="btn"
                onClick={() => rollback.mutate()}
                disabled={rollback.isPending}
              >
                Roll back to configured values
              </button>
            ) : null}
            <table className="data-table">
              <thead>
                <tr>
                  <th>Parameter</th>
                  <th>Value</th>
                  <th>Stage it acts on</th>
                </tr>
              </thead>
              <tbody>
                {parameters.data?.space.parameters.map((spec) => (
                  <tr key={spec.name}>
                    <td>
                      <code>{spec.name}</code>
                    </td>
                    <td>{active.values[spec.name]}</td>
                    <td>
                      {spec.stage}{" "}
                      <span className="muted">
                        ({spec.cost_class === "refusion" ? "free to trial" : "needs a search"})
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p className="muted">Loading…</p>
        )}
      </section>

      {histogram ? (
        <section className="tune-section">
          <h2>Where retrieval loses documents</h2>
          <p className="page__lede">{report.data?.diagnosis.summary}</p>
          <StageHistogram histogram={histogram} />
          {histogram.actionable_share !== null && histogram.actionable_share < 0.34 ? (
            <p className="tune-warning">
              Most misses are in stages no retrieval parameter reaches. Tuning is the
              wrong tool here — the failures are upstream, in what is indexed and how it
              is chunked.
            </p>
          ) : null}
        </section>
      ) : null}

      {decision ? (
        <section className="tune-section">
          <h2>Decision</h2>
          <p>
            <span className={`badge badge--${decision.outcome}`}>{decision.outcome}</span>{" "}
            {decision.reason}
          </p>
          <ul className="eval-gates">
            {decision.gates.map((gate) => (
              <GateRow key={gate.gate_id} gate={gate} />
            ))}
          </ul>
          {decision.comparisons.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Comparison</th>
                  <th>Delta</th>
                  <th>Paired queries</th>
                </tr>
              </thead>
              <tbody>
                {decision.comparisons.map((c, index) => (
                  <tr key={`${c.metric}-${index}`}>
                    <td title={c.substituted}>{c.metric}</td>
                    <td>{c.delta >= 0 ? `+${c.delta.toFixed(4)}` : c.delta.toFixed(4)}</td>
                    <td>{c.paired_queries}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </section>
      ) : null}

      {report.data?.trials?.length ? (
        <section className="tune-section">
          <h2>Trials</h2>
          <p className="muted">
            {report.data.trial_count} points evaluated with {report.data.searches} searches.
            Fusion parameters cost no retrieval at all — they are re-computed from cached
            candidates — which is why the two counts differ so much.
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>{report.data.primary_metric}</th>
                <th>Change</th>
                <th>Stage</th>
                <th>Why it was tried</th>
              </tr>
            </thead>
            <tbody>
              {(showAllTrials ? report.data.trials : report.data.trials.slice(0, 8)).map(
                (trial: TuningTrial) => (
                  <tr key={trial.trial_id}>
                    <td>{(trial.metrics[report.data!.primary_metric] ?? 0).toFixed(4)}</td>
                    <td>
                      <code>{trial.proposal.point.delta_description}</code>
                    </td>
                    <td>{trial.proposal.motivating_stage}</td>
                    <td className="muted">{trial.proposal.rationale}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
          {report.data.trials.length > 8 ? (
            <button type="button" className="btn" onClick={() => setShowAllTrials((shown) => !shown)}>
              {showAllTrials ? "Show fewer" : `Show all ${report.data.trials.length}`}
            </button>
          ) : null}
        </section>
      ) : null}

      <section className="tune-section">
        <h2>Configuration bundles</h2>
        {bundles.data?.bundles?.length ? (
          <ul className="bundles">
            {bundles.data.bundles.map((bundle) => (
              <li key={bundle.bundle_id} className={bundle.active ? "bundle bundle--live" : "bundle"}>
                <div className="bundle__head">
                  <code>{bundle.bundle_id}</code>
                  {bundle.active ? <span className="badge badge--live">live</span> : null}
                  {!bundle.active ? (
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => apply.mutate(bundle.bundle_id)}
                      disabled={apply.isPending}
                    >
                      Apply to the fleet
                    </button>
                  ) : null}
                </div>
                <div className="bundle__params">
                  {Object.entries(bundle.parameters).map(([name, value]) => (
                    <span key={name}>
                      <code>{name}</code> = {value}
                    </span>
                  ))}
                </div>
                {bundle.rationale ? <p className="muted">{bundle.rationale}</p> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            No bundles yet. A batch produces one only when a parameter set beats the
            current configuration <em>and</em> confirms on a cohort it was never selected
            on.
          </p>
        )}
      </section>
    </div>
  );
}

/**
 * The histogram, with each stage labelled by whether tuning can reach it.
 *
 * The label is the point of the component. Without it, "43% of misses are in
 * fusion" and "43% of misses are documents that were never indexed" render
 * identically and read as the same instruction.
 */
function StageHistogram({ histogram }: { histogram: TuningHistogram }) {
  const worst = Math.max(1, ...histogram.ranked.map((entry) => entry.count));
  return (
    <>
      <p className="muted">
        {histogram.evaluated} evidenced query/target pairs · {histogram.served} served ·{" "}
        {histogram.misses} missed
      </p>
      {histogram.ranked.length === 0 ? (
        <p>Every evidenced query returned its known positive. There is nothing to attribute.</p>
      ) : (
        <ul className="stages">
          {histogram.ranked.map((entry) => {
            const reachable = ACTIONABLE.has(entry.stage);
            return (
              <li key={entry.stage} className={reachable ? "stage" : "stage stage--unreachable"}>
                <div className="stage__head">
                  <strong>{entry.stage}</strong>
                  <span className="muted">
                    {entry.count} {reachable ? "· tunable" : "· not reachable by any parameter"}
                  </span>
                </div>
                <div className="stage__bar">
                  <div
                    className="stage__fill"
                    style={{ width: `${Math.round((entry.count / worst) * 100)}%` }}
                  />
                </div>
                <p className="muted">{STAGE_HELP[entry.stage] ?? ""}</p>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/** A gate that failed but does not block is a warning, not a failure. */
function GateRow({ gate }: { gate: TuningGate }) {
  const state = gate.passed ? "pass" : gate.blocking ? "fail" : "warn";
  return (
    <li className={`gate gate--${state}`}>
      <span className={`badge badge--${state}`}>{state}</span>
      <strong>{gate.gate_id}</strong>
      <span className="muted">{gate.summary}</span>
    </li>
  );
}
