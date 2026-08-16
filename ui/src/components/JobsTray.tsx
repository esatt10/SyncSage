import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { JobRecord, SourceProgress } from "../api/types";
import { ProgressBar, formatDuration, formatRate, progressCaption } from "./SyncProgress";

/**
 * Everything running in the background, with real progress.
 *
 * Before this, a multi-minute first index showed up as the word "Syncing…" and
 * nothing else — indistinguishable from a hang for exactly as long as it took.
 * The tray collapses to a one-line summary and expands to per-job phase,
 * counter and the last file each one touched.
 *
 * Phase 35.1 breaks a job down **per source**. A `sync_all` over eight sources
 * was one bar over one counter, so the one source that was stuck looked exactly
 * like the seven that were fine — and the denominator moved under your feet as
 * each source discovered its own file list.
 *
 * Polling, not SSE: the server offers `/jobs/stream`, but a poll that runs
 * *only while something is active* is simpler, survives a proxy that buffers
 * event streams, and costs nothing when the system is idle — which is almost
 * always. The stream is there for clients that want it.
 */
export function JobsTray() {
  const [expanded, setExpanded] = useState(false);
  const queryClient = useQueryClient();
  const clear = useMutation({
    mutationFn: (jobId?: string) => (jobId ? api.clearJob(jobId) : api.clearJobs()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.jobs(false),
    // Poll while work is running, and keep polling briefly after it stops so
    // the terminal state (and any error) actually renders. Idle costs nothing.
    refetchInterval: (query) => (query.state.data?.active_count ? 1000 : false),
    // A slow poll keeps the tray from being permanently blind once it has
    // gone idle — a sync started elsewhere (the scheduler, the CLI, another
    // browser tab) has to be able to wake it up again.
    refetchIntervalInBackground: false,
    staleTime: 500,
  });

  const records = jobs.data?.jobs ?? [];
  const active = records.filter((job) => job.active);
  const recent = records.filter((job) => !job.active).slice(0, 5);
  const failed = recent.filter((job) => job.status === "failed");
  const stalled = active.some((job) => job.stalled);

  if (active.length === 0 && failed.length === 0 && !expanded) {
    return null;
  }

  return (
    <div
      className={`jobs-tray${expanded ? " jobs-tray--open" : ""}${
        stalled ? " jobs-tray--stalled" : ""
      }`}
    >
      <button
        className="jobs-tray__toggle"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        title={expanded ? "Hide background work" : "Show background work"}
      >
        {active.length > 0 ? <span className="spinner" /> : null}
        <span className="jobs-tray__summary">
          {active.length > 0
            ? `${summarize(active[0])}${active.length > 1 ? ` +${active.length - 1}` : ""}`
            : failed.length > 0
              ? `${failed.length} job${failed.length === 1 ? "" : "s"} failed`
              : "Background work"}
        </span>
        <span className="jobs-tray__chevron" aria-hidden>
          {expanded ? "▾" : "▴"}
        </span>
      </button>

      {expanded ? (
        <div className="jobs-tray__body">
          {recent.length > 0 ? (
            <div className="jobs-tray__actions">
              <button className="button button--ghost" onClick={() => clear.mutate(undefined)}>
                Clear finished
              </button>
            </div>
          ) : null}
          {records.length === 0 ? (
            <p className="muted small" style={{ margin: 0 }}>
              Nothing running. Syncs, uploads and re-indexes show up here with
              live progress.
            </p>
          ) : null}
          {[...active, ...recent].map((job) => (
            <JobRow key={job.id} job={job} onClear={() => clear.mutate(job.id)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function JobRow({ job, onClear }: { job: JobRecord; onClear: () => void }) {
  // Sources are only broken out when there is more than one: for a single
  // source the rollup and the source row carry identical numbers, and showing
  // both is noise.
  const sources = job.sources ?? [];
  const perSource = sources.length > 1;

  return (
    <div className={`job job--${job.status}${job.stalled ? " job--stalled" : ""}`}>
      <div className="job__head">
        <span className="job__label">{job.label}</span>
        <span className="job__status">{job.active ? job.progress.phase : job.status}</span>
        {!job.active ? (
          <button
            className="job__clear"
            onClick={onClear}
            aria-label={`Clear ${job.label}`}
            title="Clear"
          >
            ×
          </button>
        ) : null}
      </div>

      <ProgressBar fraction={job.progress.fraction} stalled={job.stalled} />

      <div className="job__detail muted small">
        {job.error ? (
          <span className="job__error">{job.error}</span>
        ) : perSource ? (
          <>
            {sources.filter((row) => !row.active).length}/{sources.length} sources done
          </>
        ) : (
          <>
            {job.progress.total
              ? `${job.progress.current} / ${job.progress.total}`
              : job.progress.current
                ? `${job.progress.current}`
                : null}
            {sources[0] ? <> · {progressCaption(sources[0])}</> : null}
            {job.progress.detail ? ` · ${job.progress.detail}` : null}
          </>
        )}
      </div>

      {perSource ? (
        <div className="job__sources">
          {sources.map((row) => (
            <SourceRow key={row.source} row={row} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SourceRow({ row }: { row: SourceProgress }) {
  return (
    <div className={`job-source${row.stalled ? " job-source--stalled" : ""}`}>
      <div className="job-source__head">
        <span className="job-source__name">{row.source}</span>
        <span className="job-source__phase muted small">
          {row.active ? row.phase : row.status}
          {row.total ? ` ${row.current}/${row.total}` : null}
        </span>
      </div>
      <ProgressBar fraction={row.fraction} stalled={row.stalled} compact />
      <div className="job-source__meta muted small">{progressCaption(row)}</div>
    </div>
  );
}

function summarize(job: JobRecord): string {
  const { current, total, phase } = job.progress;
  const rate = job.sources?.find((row) => row.active && row.files_per_second);
  const suffix = rate?.files_per_second ? ` · ${formatRate(rate.files_per_second)}` : "";
  const eta = rate?.eta_seconds ? ` · ${formatDuration(rate.eta_seconds)} left` : "";
  if (total) return `${job.label} — ${phase} ${current}/${total}${suffix}${eta}`;
  return `${job.label} — ${phase}${suffix}`;
}
