import type {
  EvaluationClassification,
  EvaluationGate,
  EvaluationHealthEntry,
  EvaluationMetricResult,
  EvaluationMetricStatus,
  EvaluationTrendPoint,
} from "../api/types";

/**
 * The rendering rules that keep an evaluation number honest on screen.
 *
 * Three of them, and each exists because the alternative is a lie a reader
 * cannot see:
 *
 * 1. **`value: null` renders as a gap, never as a zero.** A metric with a
 *    status of `insufficient_evidence` was not measured; drawing it as 0.00
 *    puts a red bar on the dashboard describing an instrumentation gap, and
 *    teaches people to ignore red bars.
 * 2. **A value never appears without its denominator.** "0.89" and "0.89 over
 *    5 of 103 evidenced queries" are different claims, and only the second one
 *    is true.
 * 3. **Classification is always visible.** `demonstrated`, `structural`,
 *    `diagnostic` and `operational` are different kinds of claim; a reader who
 *    cannot see which they are holding will read corpus-relative geometry as
 *    factual accuracy.
 */

export const STATUS_LABEL: Record<EvaluationMetricStatus, string> = {
  pass: "pass",
  warn: "warn",
  fail: "fail",
  informational: "measured",
  insufficient_evidence: "not enough evidence",
  not_applicable: "not applicable",
};

export const CLASSIFICATION_HELP: Record<EvaluationClassification, string> = {
  demonstrated: "Backed by recorded interaction proof, structured facts or downstream outcomes.",
  structural: "Deterministic system behaviour. Says what the machinery did, not whether it helped.",
  diagnostic:
    "Corpus-relative proximity or consensus. Never a factual-accuracy claim, however high it is.",
  operational: "What this cost: latency, storage, computation.",
};

/** Metrics where a lower number is the better one. */
const LOWER_IS_BETTER = new Set([
  "known_negative_exposure_at_5",
  "control_regression",
  "stale_memory_leak",
  "generalization_gap",
]);

/** Metrics that are a signed change rather than a level. */
const IS_DELTA = new Set([
  "memory_attributable_gain",
  "future_query_generalization",
  "generalization_gap",
]);

export function formatValue(name: string, entry: EvaluationHealthEntry): string {
  if (entry.value === null || entry.value === undefined) return "—";
  if (IS_DELTA.has(name)) return entry.value >= 0 ? `+${entry.value.toFixed(3)}` : entry.value.toFixed(3);
  return entry.value.toFixed(3);
}

export function denominatorLabel(entry: EvaluationHealthEntry): string | null {
  if (entry.denominator === null || entry.denominator === undefined) return null;
  if (entry.numerator === null || entry.numerator === undefined) {
    return `n=${entry.denominator}`;
  }
  return `${entry.numerator} / ${entry.denominator}`;
}

export function toneFor(name: string, entry: EvaluationHealthEntry): string {
  if (entry.value === null || entry.value === undefined) return "muted";
  if (entry.status === "fail") return "bad";
  if (entry.status === "pass") return "good";
  if (!IS_DELTA.has(name)) return "neutral";
  const better = LOWER_IS_BETTER.has(name) ? entry.value < 0 : entry.value > 0;
  if (Math.abs(entry.value) < 1e-9) return "neutral";
  return better ? "good" : "bad";
}

export function HealthTile({ name, entry }: { name: string; entry: EvaluationHealthEntry }) {
  const denominator = denominatorLabel(entry);
  const unmeasured = entry.value === null || entry.value === undefined;
  return (
    <div className={`eval-tile eval-tile--${toneFor(name, entry)}`}>
      <div className="eval-tile__name">{name.replace(/_/g, " ")}</div>
      <div className="eval-tile__value">{formatValue(name, entry)}</div>
      <div className="eval-tile__meta">
        {/* The denominator is not decoration. A score without it is the
            artifact this whole plane exists to avoid publishing. */}
        {denominator ? <span className="eval-tile__denominator">{denominator}</span> : null}
        <span
          className={`eval-chip eval-chip--${entry.status}`}
          title={entry.classification ? CLASSIFICATION_HELP[entry.classification] : undefined}
        >
          {STATUS_LABEL[entry.status] ?? entry.status}
        </span>
      </div>
      {unmeasured ? (
        <div className="eval-tile__note">
          Not measured — this is a gap in the evidence, not a score of zero.
        </div>
      ) : null}
    </div>
  );
}

export function GateRow({ gate }: { gate: EvaluationGate }) {
  return (
    <li className={`eval-gate ${gate.passed ? "eval-gate--pass" : "eval-gate--fail"}`}>
      <span className="eval-gate__mark" aria-hidden>
        {gate.passed ? "✓" : "✕"}
      </span>
      <span className="eval-gate__id">{gate.gate_id.replace(/_/g, " ")}</span>
      <span className="eval-gate__detail">{gate.detail}</span>
    </li>
  );
}

/**
 * A trend line drawn as an inline SVG.
 *
 * Deliberately no charting dependency: this is one series of at most a few
 * dozen points, and the UI bundle is baked into the container image. Points a
 * run could not measure are *breaks* in the line rather than zeros, for the
 * same reason the tiles render them as gaps.
 */
export function Sparkline({
  points,
  height = 56,
}: {
  points: EvaluationTrendPoint[];
  height?: number;
}) {
  const measured = points.filter((point) => point.value !== null);
  if (measured.length < 2) {
    return (
      <p className="muted small">
        {measured.length === 0
          ? "No measured points yet."
          : "One measured point so far — a trend needs at least two."}
      </p>
    );
  }
  const values = measured.map((point) => point.value as number);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const span = max - min || 1;
  const width = 100;
  const step = width / Math.max(1, points.length - 1);
  const segments: string[] = [];
  let current: string[] = [];
  points.forEach((point, index) => {
    if (point.value === null) {
      if (current.length > 1) segments.push(current.join(" "));
      current = [];
      return;
    }
    const x = index * step;
    const y = height - ((point.value - min) / span) * (height - 8) - 4;
    current.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  });
  if (current.length > 1) segments.push(current.join(" "));

  return (
    <svg
      className="eval-spark"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Trend across ${points.length} snapshots, ${measured.length} of them measured`}
    >
      {segments.map((segment, index) => (
        <polyline key={index} points={segment} fill="none" strokeWidth="1.6" />
      ))}
      {points.map((point, index) =>
        point.value === null ? null : (
          <circle
            key={point.run_id + index}
            cx={index * step}
            cy={height - ((point.value - min) / span) * (height - 8) - 4}
            r="1.8"
          >
            <title>{`${point.started_at}: ${point.value.toFixed(4)} (${point.numerator ?? "?"}/${point.denominator ?? "?"})`}</title>
          </circle>
        ),
      )}
    </svg>
  );
}

/**
 * One metric, opened up: the formula, the numbers substituted into it, the
 * operands, and the limitation.
 *
 * This is the whole audit trail the specification asks for, and it is why the
 * UI stores the metric payload rather than just its value — a reader who
 * disagrees with a number needs to see what went into it without leaving the
 * page.
 */
export function MetricDetail({ metric }: { metric: EvaluationMetricResult }) {
  const { result, calculation, evidence, interpretation } = metric;
  return (
    <div className="eval-detail">
      <dl className="eval-detail__grid">
        <dt>Formula</dt>
        <dd>
          <code>{calculation.formula}</code>
        </dd>
        <dt>Substituted</dt>
        <dd>
          <code>{calculation.substituted}</code>
        </dd>
        <dt>Result</dt>
        <dd>
          {result.value === null ? "not measured" : result.value.toFixed(4)}
          {result.denominator !== null ? ` over ${result.denominator}` : ""} · {result.status}
        </dd>
        {evidence.excluded_count > 0 ? (
          <>
            <dt>Excluded</dt>
            <dd>
              {evidence.excluded_count} —{" "}
              {Object.entries(evidence.exclusion_reasons)
                .map(([reason, count]) => `${reason.replace(/_/g, " ")}: ${count}`)
                .join(", ")}
            </dd>
          </>
        ) : null}
        {evidence.proof_ids.length ? (
          <>
            <dt>Proof</dt>
            <dd>{evidence.proof_ids.length} recorded observation(s)</dd>
          </>
        ) : null}
      </dl>
      <p className="eval-detail__summary">{interpretation.summary}</p>
      <p className="eval-detail__limit">
        <strong>Does not support:</strong> {interpretation.does_not_support}
      </p>
    </div>
  );
}
