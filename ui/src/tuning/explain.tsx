import { useState } from "react";
import type { GlossaryEntry } from "../api/types";

/**
 * Explanation where the number is, not where the docs are.
 *
 * The failure this exists to prevent is specific: a dashboard of rates read
 * confidently and wrongly. A 42% truncation rate looks alarming and is
 * normal. A 0% empty rate looks fine and proves nothing about quality. A
 * `generalization_gap` sounds like a bug and is supposed to be non-zero.
 * Someone reading those without their meaning acts on the ones that *sound*
 * alarming rather than the ones that are.
 *
 * So every measure on this page can be expanded in place, and the expansion
 * leads with what the number means, then what to do if it moves, then — set
 * apart, because it is the part people skip — the misreading it invites.
 *
 * Deliberately a disclosure rather than a tooltip. Tooltips are unreachable
 * by keyboard on most implementations, vanish while you are reading them, and
 * cannot hold three paragraphs. This is a `<details>`: it works without
 * JavaScript, it is focusable, and it stays open while the reader looks
 * between it and the number.
 */
export function Explain({ entry, compact }: { entry?: GlossaryEntry; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!entry) return null;
  return (
    <details
      className={compact ? "explain explain--compact" : "explain"}
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary aria-label={`What ${entry.label} means`}>
        {compact ? "?" : "What does this mean?"}
      </summary>
      <div className="explain__body">
        <p>{entry.means}</p>
        <p>
          <strong>What to do if it moves.</strong> {entry.impact}
        </p>
        <p className="explain__caveat">
          <strong>What it does not mean.</strong> {entry.does_not_mean}
        </p>
      </div>
    </details>
  );
}

/**
 * The objective, stated with its trade.
 *
 * An objective without a stated trade is a preference presented as an
 * optimum, so this never renders the name alone. A reader who sees
 * "recall_at_10" and not "will accept a worse rank-1 in exchange" has been
 * told the name of a decision without its content.
 */
export function ObjectiveBanner({
  objective,
  baselineSubstituted,
}: {
  objective?: {
    label: string;
    objective_id: string;
    summary: string;
    trades_away: string;
    weights: Record<string, number>;
  };
  baselineSubstituted?: string;
}) {
  if (!objective) return null;
  const composite = Object.keys(objective.weights).length > 1;
  return (
    <div className="objective">
      <div className="objective__head">
        <span className="objective__label">Tuning for</span>
        <strong>{objective.label}</strong>
        <code className="muted small">{objective.objective_id}</code>
      </div>
      <p className="muted small">{objective.summary}</p>
      <p className="objective__trade small">
        <strong>Trades away:</strong> {objective.trades_away}
      </p>
      {composite ? (
        <p className="muted small">
          Weighted:{" "}
          {Object.entries(objective.weights)
            .map(([metric, weight]) => `${weight.toFixed(2)} x ${metric}`)
            .join(" + ")}
        </p>
      ) : null}
      {baselineSubstituted ? (
        <p className="muted small">Current configuration scores {baselineSubstituted}</p>
      ) : null}
    </div>
  );
}

/**
 * Each retrieval mechanism scored on its own, against the merge.
 *
 * "Hybrid is better" is an assumption most regions never test and it is
 * frequently false — a vector index that was never built contributes nothing
 * and costs latency on every search. A negative `hybrid_gain` means the merge
 * is *losing* to a single mechanism on this cohort, which is a finding worth
 * saying in words rather than leaving to be inferred from two numbers.
 */
export function MechanismTable({
  mechanisms,
  objectiveLabel,
}: {
  mechanisms?: Record<
    string,
    { objective_score: number | null; evaluated_queries: number; hybrid_gain?: number }
  >;
  objectiveLabel: string;
}) {
  if (!mechanisms || !Object.keys(mechanisms).length) return null;
  const hybrid = mechanisms.hybrid;
  const arms = Object.entries(mechanisms).filter(([name]) => name !== "hybrid");
  const beating = arms.filter(([, m]) => (m.hybrid_gain ?? 0) < 0);
  return (
    <div className="mechanisms">
      <table className="data-table">
        <thead>
          <tr>
            <th>Mechanism</th>
            <th>{objectiveLabel}</th>
            <th>What the merge adds</th>
          </tr>
        </thead>
        <tbody>
          {arms.map(([name, m]) => (
            <tr key={name}>
              <td>
                <code>{name}</code> arm alone
              </td>
              <td>{m.objective_score === null ? "—" : m.objective_score.toFixed(4)}</td>
              <td className={(m.hybrid_gain ?? 0) < 0 ? "mechanisms__loss" : undefined}>
                {m.hybrid_gain === undefined
                  ? "—"
                  : `${m.hybrid_gain >= 0 ? "+" : ""}${m.hybrid_gain.toFixed(4)}`}
              </td>
            </tr>
          ))}
          {hybrid ? (
            <tr className="mechanisms__hybrid">
              <td>
                <strong>hybrid</strong> (what is served)
              </td>
              <td>
                {hybrid.objective_score === null ? "—" : hybrid.objective_score.toFixed(4)}
              </td>
              <td className="muted">over {hybrid.evaluated_queries} queries</td>
            </tr>
          ) : null}
        </tbody>
      </table>
      {beating.length ? (
        <p className="tune-warning">
          On this cohort the {beating.map(([n]) => n).join(" and ")} arm
          {beating.length > 1 ? "s" : ""} alone score
          {beating.length > 1 ? "" : "s"} <em>above</em> the merge. Hybrid retrieval is
          costing latency without earning it here — worth checking the arm weights, or
          whether the other arms have a usable index at all.
        </p>
      ) : (
        <p className="muted small">
          Measured by re-fusing the captured candidates with the other arms weighted to
          zero, so this costs no extra retrieval. Reported, never acted on automatically:
          dropping an arm has consequences beyond one cohort.
        </p>
      )}
    </div>
  );
}
