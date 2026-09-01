import { useId, useState } from "react";
import type { TuningHistogram, TuningSweepPoint } from "../api/types";

/**
 * Charts for the tuning plane, as inline SVG against the app's own tokens.
 *
 * Three deliberate choices, each of which is the difference between a chart
 * that informs and one that merely decorates:
 *
 * **Small multiples, never a shared axis.** A sweep asks "what happens to the
 * score as *this* parameter moves". Two parameters on one plot would need two
 * x-scales, which is the dual-axis mistake wearing a different hat: the
 * crossing point would be an artifact of the scales, and people read crossings
 * as meaning. One parameter, one chart, one axis.
 *
 * **Reachability is a labelled status, not a hue.** The stage histogram's
 * whole job is to separate "the fusion demoted it" from "it was never
 * indexed". Those are not two series in a palette, they are two *kinds of
 * finding* with opposite instructions, so each bar carries the word as well as
 * the colour and nothing depends on telling olive from tan.
 *
 * **Every mark has a hover.** An SVG chart is interactive by default, and the
 * number behind a dot is the thing a reader wants next. Values are never
 * printed on every point — only the baseline and the best are direct-labelled,
 * because a scatter with a number on each dot is a table drawn badly.
 *
 * No chart library: the app carries cytoscape for the graph and nothing else,
 * and a sweep is thirty lines of path arithmetic.
 */

const AXIS = "var(--border-strong)";
const GRID = "var(--graph-grid)";
const INK = "var(--muted)";

function niceExtent(values: number[]): [number, number] {
  const finite = values.filter((v) => Number.isFinite(v));
  if (!finite.length) return [0, 1];
  let low = Math.min(...finite);
  let high = Math.max(...finite);
  if (low === high) {
    // A flat series still deserves a readable band rather than a divide-by-
    // zero: pad by a tenth, or by 0.5 when the value itself is zero.
    const pad = Math.abs(low) * 0.1 || 0.5;
    low -= pad;
    high += pad;
  }
  return [low, high];
}

export interface SweepChartProps {
  parameter: string;
  stage: string;
  metric: string;
  points: TuningSweepPoint[];
  baseline?: number | null;
}

/**
 * One parameter's ladder against the primary metric.
 *
 * The baseline is drawn as a reference line rather than as another point,
 * because "what we serve today" is the thing every other value is being
 * compared *to* — plotting it as a peer invites reading the best point as the
 * answer when the honest question is whether it beat the line.
 */
export function SweepChart({ parameter, stage, metric, points, baseline }: SweepChartProps) {
  const [hover, setHover] = useState<number | null>(null);
  const clipId = useId();
  // Narrowed to a concrete shape rather than asserted: a point missing either
  // coordinate cannot be plotted, and a cast would let one through as a NaN
  // that renders as a mark somewhere arbitrary.
  const usable: { trial_id: string; value: number; metric: number; cost_class: string }[] =
    points.flatMap((p) =>
      typeof p.value === "number" && typeof p.metric === "number"
        ? [
            {
              trial_id: p.trial_id,
              value: p.value,
              metric: p.metric,
              cost_class: p.cost_class,
            },
          ]
        : [],
    );
  if (usable.length < 2) return null;

  const width = 320;
  const height = 150;
  // The right margin holds the baseline's "now" label. Sized to fit it
  // *outside* the plot: drawn inside, it collided with any point that landed
  // near the right edge, which is exactly where the highest parameter value
  // sits — so the collision showed up on the charts that mattered most.
  const pad = { top: 14, right: 32, bottom: 28, left: 42 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const [xLow, xHigh] = niceExtent(usable.map((p) => p.value));
  const [yLow, yHigh] = niceExtent([
    ...usable.map((p) => p.metric),
    ...(typeof baseline === "number" ? [baseline] : []),
  ]);
  const x = (v: number) => pad.left + ((v - xLow) / (xHigh - xLow || 1)) * plotW;
  const y = (v: number) => pad.top + plotH - ((v - yLow) / (yHigh - yLow || 1)) * plotH;

  const path = usable.map((p, i) => `${i ? "L" : "M"}${x(p.value)},${y(p.metric)}`).join(" ");
  const best = usable.reduce((a, b) => (b.metric > a.metric ? b : a));
  const active = hover === null ? null : usable[hover];

  return (
    <figure className="sweep">
      <figcaption>
        <strong>{parameter}</strong>
        <span className="muted small"> · {stage}</span>
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${metric} as ${parameter} varies`}
      >
        <defs>
          <clipPath id={clipId}>
            <rect x={pad.left} y={pad.top} width={plotW} height={plotH} />
          </clipPath>
        </defs>
        {/* Recessive grid: three gridlines, no box. */}
        {[0, 0.5, 1].map((t) => (
          <line
            key={t}
            x1={pad.left}
            x2={pad.left + plotW}
            y1={pad.top + plotH * t}
            y2={pad.top + plotH * t}
            stroke={GRID}
            strokeWidth={1}
          />
        ))}
        <line
          x1={pad.left}
          x2={pad.left}
          y1={pad.top}
          y2={pad.top + plotH}
          stroke={AXIS}
          strokeWidth={1}
        />
        {typeof baseline === "number" && baseline >= yLow && baseline <= yHigh ? (
          <>
            <line
              x1={pad.left}
              x2={pad.left + plotW}
              y1={y(baseline)}
              y2={y(baseline)}
              stroke={AXIS}
              strokeWidth={1}
              strokeDasharray="4 3"
            />
            <text
              x={width - 3}
              y={y(baseline) + 3}
              fill={INK}
              fontSize={9}
              textAnchor="end"
            >
              now
            </text>
          </>
        ) : null}
        <path
          d={path}
          fill="none"
          stroke="var(--accent)"
          strokeWidth={2}
          clipPath={`url(#${clipId})`}
        />
        {usable.map((p, i) => (
          <g key={p.trial_id || i}>
            <circle
              cx={x(p.value)}
              cy={y(p.metric)}
              r={p === best ? 5 : 4}
              fill={p === best ? "var(--accent)" : "var(--bg-raised)"}
              stroke="var(--accent)"
              strokeWidth={2}
            />
            {/* Hit target larger than the mark, per the interaction rules. */}
            <circle
              cx={x(p.value)}
              cy={y(p.metric)}
              r={11}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          </g>
        ))}
        <text x={pad.left} y={height - 8} fill={INK} fontSize={9}>
          {xLow.toPrecision(3)}
        </text>
        <text x={pad.left + plotW} y={height - 8} fill={INK} fontSize={9} textAnchor="end">
          {xHigh.toPrecision(3)}
        </text>
        <text x={pad.left - 6} y={pad.top + 4} fill={INK} fontSize={9} textAnchor="end">
          {yHigh.toFixed(2)}
        </text>
        <text x={pad.left - 6} y={pad.top + plotH} fill={INK} fontSize={9} textAnchor="end">
          {yLow.toFixed(2)}
        </text>
      </svg>
      <div className="sweep__readout">
        {active ? (
          <span>
            <code>
              {parameter} = {active.value}
            </code>{" "}
            → {active.metric.toFixed(4)}
            <span className="muted small"> ({active.cost_class})</span>
          </span>
        ) : (
          <span className="muted small">
            best {best.value} → {best.metric.toFixed(4)} · hover a point
          </span>
        )}
      </div>
    </figure>
  );
}

/**
 * Where retrieval loses documents, by stage.
 *
 * Bars are ordered by count, and each carries the word "tunable" or "not
 * reachable" beside it. That label is not redundant with the colour — it is
 * the primary encoding, and the colour is the reinforcement. A reader who
 * cannot separate the two hues loses nothing.
 */
export function StageBars({
  histogram,
  actionable,
  help,
}: {
  histogram: TuningHistogram;
  actionable: Set<string>;
  help: Record<string, string>;
}) {
  const ranked = histogram.ranked ?? [];
  const worst = Math.max(1, ...ranked.map((e) => e.count));
  if (!ranked.length) {
    return (
      <p className="muted">
        Every evidenced query returned its known positive. There is nothing to attribute.
      </p>
    );
  }
  return (
    <ul className="stages">
      {ranked.map((entry) => {
        const reachable = actionable.has(entry.stage);
        return (
          <li key={entry.stage} className={reachable ? "stage" : "stage stage--unreachable"}>
            <div className="stage__head">
              <strong>{entry.stage}</strong>
              <span className="muted small">
                {entry.count} · {reachable ? "tunable" : "not reachable by any parameter"}
              </span>
            </div>
            <div className="stage__bar">
              <div
                className="stage__fill"
                style={{ width: `${Math.round((entry.count / worst) * 100)}%` }}
                title={`${entry.count} of ${histogram.misses} misses`}
              />
            </div>
            <p className="muted small">{help[entry.stage] ?? ""}</p>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * A rate with its denominator, always.
 *
 * `value === null` renders as a gap rather than a zero — the same rule the
 * evaluation tiles follow, and for the same reason: a metric that was not
 * measured drawn as 0% puts a bar on the dashboard describing an
 * instrumentation gap and teaches people to ignore bars.
 */
export function RateTile({
  label,
  value,
  denominator,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: number | null;
  denominator: string;
  hint?: string;
  tone?: "neutral" | "good" | "bad";
}) {
  return (
    <div className={`rate-tile rate-tile--${tone}`}>
      <div className="rate-tile__label">{label}</div>
      <div className="rate-tile__value">
        {value === null ? <span className="muted">—</span> : `${(value * 100).toFixed(1)}%`}
      </div>
      <div className="rate-tile__denominator muted small">{denominator}</div>
      {hint ? <div className="rate-tile__hint muted small">{hint}</div> : null}
    </div>
  );
}
