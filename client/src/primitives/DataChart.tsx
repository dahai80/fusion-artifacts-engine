// Port of visual_primitives/data_chart.py (issue #63).
// 4-5年级 统计/条形/折线。
//
// Deviation from the Python compiler: the server side renders via Chart.js
// (CDN <script>); the React layer draws the bar/line chart natively in SVG
// (dependency-free, SSR-safe). Bar heights scale with progress like the
// server-side value scaling.

import { metaOf, parametersOf, pipelineOf, titleOf } from "../helpers";
import { COLOR_BASE, type PrimitiveProps } from "./shared";

const W = 600;
const H = 400;
const M = { top: 30, right: 30, bottom: 40, left: 50 };

export function DataChart({ dsl, progress }: PrimitiveProps) {
  const params = parametersOf(dsl);
  const pipeline = pipelineOf(dsl);
  const meta = metaOf(dsl);
  const topicHint = String(meta.topic ?? "").toLowerCase();
  const chartType = topicHint.includes("折线") || topicHint.includes("line") ? "line" : "bar";

  let labels: string[] = [];
  let values: number[] = [];
  for (const [key, val] of Object.entries(params)) {
    if (!val || typeof val !== "object") continue;
    const v = val.value;
    if (typeof v === "number" && Number.isFinite(v)) {
      labels.push(String(val.label ?? key));
      values.push(v);
    }
  }
  if (labels.length === 0) {
    pipeline.forEach((step, i) => {
      labels.push(String(step?.title ?? `步骤${i + 1}`));
      values.push(i + 1);
    });
  }
  if (labels.length === 0) {
    labels = ["A", "B", "C", "D"];
    values = [10, 20, 15, 25];
  }
  const scaled = values.map((v) => Math.round(v * Math.max(0, Math.min(1, progress)) * 100) / 100);
  const maxV = Math.max(...scaled, 1);

  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom;
  const bandW = innerW / scaled.length;
  const yFor = (v: number): number => M.top + innerH - (v / maxV) * innerH;

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const y = M.top + innerH - f * innerH;
    return (
      <g key={`grid-${f}`}>
        <line x1={M.left} y1={y} x2={W - M.right} y2={y} stroke="#e9ecef" strokeWidth={1} />
        <text x={M.left - 8} y={y + 4} textAnchor="end" fontSize={11} fill="#868e96">
          {Math.round(f * maxV)}
        </text>
      </g>
    );
  });

  let marks: React.ReactNode = null;
  if (chartType === "bar") {
    marks = scaled.map((v, i) => (
      <rect
        key={`bar-${i}`}
        x={M.left + i * bandW + bandW * 0.15}
        y={yFor(v)}
        width={bandW * 0.7}
        height={M.top + innerH - yFor(v)}
        fill={COLOR_BASE}
        className="fae-bar"
      />
    ));
  } else {
    const pts = scaled.map((v, i) => [M.left + i * bandW + bandW / 2, yFor(v)] as const);
    marks = (
      <g>
        <polyline
          points={pts.map(([x, y]) => `${x},${y}`).join(" ")}
          fill="none"
          stroke={COLOR_BASE}
          strokeWidth={2}
          className="fae-line"
        />
        {pts.map(([x, y], i) => (
          <circle key={`pt-${i}`} cx={x} cy={y} r={4} fill={COLOR_BASE} />
        ))}
      </g>
    );
  }

  return (
    <div className="fae-primitive fae-data-chart">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img">
        {gridLines}
        {marks}
        {labels.map((l, i) => (
          <text
            key={`label-${i}`}
            x={M.left + i * bandW + bandW / 2}
            y={H - M.bottom + 18}
            textAnchor="middle"
            fontSize={12}
            fill="#495057"
          >
            {l}
          </text>
        ))}
      </svg>
    </div>
  );
}
