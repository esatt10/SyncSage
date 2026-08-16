import type { SourceProgress } from "../api/types";

/**
 * Shared rendering for indexing progress (Phase 35.1).
 *
 * One module because the jobs tray and the Sources page must not disagree
 * about what "stalled" looks like or how a rate is rounded — a source shown as
 * healthy in one place and stuck in another is worse than either answer alone.
 */

/** Seconds → a short human duration. Deliberately coarse: an ETA derived from
 *  a sliding rate window is an estimate, and "1h 20m" claims exactly as much
 *  precision as it has. "4,812s" claims far more. */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

export function formatRate(filesPerSecond: number): string {
  if (!Number.isFinite(filesPerSecond) || filesPerSecond <= 0) return "—";
  if (filesPerSecond < 1) return `${(filesPerSecond * 60).toFixed(0)}/min`;
  return `${filesPerSecond.toFixed(1)}/s`;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / 1024 ** exponent;
  return `${value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[exponent]}`;
}

/**
 * The one line that answers "is this working?".
 *
 * Ordered by what a watcher actually wants: the alarm first if there is one,
 * then throughput and time remaining, then the evidence of work done. The
 * skipped count matters more than it looks — on an incremental pass
 * "2,996 unchanged · 4 indexed" is the difference between "working correctly"
 * and "re-indexing everything again", and nothing in the UI said which.
 */
export function progressCaption(row: SourceProgress): string {
  const parts: string[] = [];
  if (row.stalled) {
    parts.push(`no progress for ${formatDuration(row.seconds_since_progress)}`);
  } else if (row.active && row.seconds_since_progress > 10) {
    parts.push(`last update ${formatDuration(row.seconds_since_progress)} ago`);
  }
  if (row.active && row.files_per_second) parts.push(formatRate(row.files_per_second));
  if (row.active && row.eta_seconds) parts.push(`${formatDuration(row.eta_seconds)} left`);
  if (row.indexed) parts.push(`${row.indexed.toLocaleString()} indexed`);
  if (row.skipped) parts.push(`${row.skipped.toLocaleString()} unchanged`);
  if (row.failed) parts.push(`${row.failed.toLocaleString()} failed`);
  if (row.bytes_done) parts.push(formatBytes(row.bytes_done));
  return parts.join(" · ");
}

/**
 * An unknown total renders as an indeterminate bar rather than 0%: a sync does
 * not know how many files it will index until the connector has finished
 * listing, and a bar sitting at zero for that whole period reads as "stuck".
 */
export function ProgressBar({
  fraction,
  stalled = false,
  compact = false,
}: {
  fraction: number | null;
  stalled?: boolean;
  compact?: boolean;
}) {
  const percent = fraction === null ? undefined : Math.round(fraction * 100);
  return (
    <div
      className={[
        "job__bar",
        fraction === null ? "job__bar--indeterminate" : "",
        stalled ? "job__bar--stalled" : "",
        compact ? "job__bar--compact" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      role="progressbar"
      aria-valuenow={percent}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <span
        className="job__bar-fill"
        style={percent === undefined ? undefined : { width: `${percent}%` }}
      />
    </div>
  );
}

/** The compact per-source block used on the Sources page. */
export function SourceSyncProgress({ row }: { row: SourceProgress }) {
  return (
    <div className={`source-progress${row.stalled ? " source-progress--stalled" : ""}`}>
      <div className="source-progress__head small">
        <span className="source-progress__phase">
          {row.active ? row.phase : row.status}
          {row.total ? ` ${row.current.toLocaleString()}/${row.total.toLocaleString()}` : null}
        </span>
        {row.stalled ? <span className="badge badge--warn">stalled</span> : null}
      </div>
      <ProgressBar fraction={row.fraction} stalled={row.stalled} compact />
      <div className="source-progress__meta muted small">{progressCaption(row)}</div>
      {row.failures.length > 0 ? (
        <ul className="source-progress__failures small">
          {row.failures.slice(0, 5).map((failure) => (
            <li key={failure.path}>
              <code>{failure.path}</code> — {failure.error}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
