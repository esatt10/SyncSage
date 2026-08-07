import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { JobRecord } from "../api/types";

/**
 * Everything running in the background, with real progress.
 *
 * Before this, a multi-minute first index showed up as the word "Syncing…" and
 * nothing else — indistinguishable from a hang for exactly as long as it took.
 * The tray collapses to a one-line summary and expands to per-job phase,
 * counter and the last file each one touched.
 *
 * Polling, not SSE: the server offers `/jobs/stream`, but a poll that runs
 * *only while something is active* is simpler, survives a proxy that buffers
 * event streams, and costs nothing when the system is idle — which is almost
 * always. The stream is there for clients that want it.
 */
export function JobsTray() {
  const [expanded, setExpanded] = useState(false);

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

  if (active.length === 0 && failed.length === 0 && !expanded) {
    return null;
  }

  return (
    <div className={`jobs-tray${expanded ? " jobs-tray--open" : ""}`}>
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
          {records.length === 0 ? (
            <p className="muted small" style={{ margin: 0 }}>
              Nothing running. Syncs, uploads and re-indexes show up here with
              live progress.
            </p>
          ) : null}
          {[...active, ...recent].map((job) => (
            <JobRow key={job.id} job={job} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function JobRow({ job }: { job: JobRecord }) {
  const fraction = job.progress.fraction;
  return (
    <div className={`job job--${job.status}`}>
      <div className="job__head">
        <span className="job__label">{job.label}</span>
        <span className="job__status">
          {job.active ? job.progress.phase : job.status}
        </span>
      </div>

      {/* An unknown total renders as an indeterminate bar rather than 0%:
          a sync does not know how many files it will index until the
          connector has finished listing, and a bar sitting at zero for that
          whole period reads as "stuck". */}
      <div
        className={`job__bar${fraction === null ? " job__bar--indeterminate" : ""}`}
        role="progressbar"
        aria-valuenow={fraction === null ? undefined : Math.round(fraction * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span
          className="job__bar-fill"
          style={fraction === null ? undefined : { width: `${Math.round(fraction * 100)}%` }}
        />
      </div>

      <div className="job__detail muted small">
        {job.error ? (
          <span className="job__error">{job.error}</span>
        ) : (
          <>
            {job.progress.total
              ? `${job.progress.current} / ${job.progress.total}`
              : job.progress.current
                ? `${job.progress.current}`
                : null}
            {job.progress.detail ? ` · ${job.progress.detail}` : null}
          </>
        )}
      </div>
    </div>
  );
}

function summarize(job: JobRecord): string {
  const { current, total, phase } = job.progress;
  if (total) return `${job.label} — ${phase} ${current}/${total}`;
  return `${job.label} — ${phase}`;
}
