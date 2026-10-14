// Port of visual_primitives/tape_diagram.py (issue #63).
// 2-6年级 和差倍比/分数。条带/分段图，核心兜底基元。

import { activeEntities, parametersOf, titleOf } from "../helpers";
import { PALETTE, type PrimitiveProps } from "./shared";

const BAR_W = 600;
const BAR_H = 60;

export function TapeDiagram({ dsl, progress }: PrimitiveProps) {
  const params = parametersOf(dsl);
  const segments: Array<[string, number]> = [];
  for (const [key, val] of Object.entries(params)) {
    if (!val || typeof val !== "object") continue;
    const v = val.value;
    if (typeof v === "number" && Number.isFinite(v) && v >= 0) {
      segments.push([String(val.label ?? key), v]);
    }
  }
  if (segments.length === 0) {
    segments.push(["A", 1], ["B", 1]);
  }
  const total = segments.reduce((s, [, v]) => s + v, 0) || 1;
  const active = activeEntities(dsl, progress);

  const parts: React.ReactNode[] = [];
  let x = 0;
  segments.forEach(([label, val], i) => {
    const segW = (val / total) * BAR_W;
    const entId = `seg_${i}`;
    parts.push(
      <rect
        key={entId}
        x={x}
        y={0}
        width={segW}
        height={BAR_H}
        fill={PALETTE[i % PALETTE.length]}
        opacity={active.has(entId) ? 1.0 : 0.7}
        className="fae-seg"
        data-id={entId}
      />,
    );
    if (segW > 30) {
      parts.push(
        <text
          key={`${entId}-label`}
          x={x + segW / 2}
          y={BAR_H / 2}
          textAnchor="middle"
          dy=".35em"
          fontSize={13}
          fill="#fff"
        >
          {label}: {val}
        </text>,
      );
    }
    x += segW;
  });

  return (
    <div className="fae-primitive fae-tape-diagram">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={BAR_W} height={BAR_H + 40} viewBox={`0 0 ${BAR_W} ${BAR_H + 40}`} role="img">
        <g transform="translate(0,10)">{parts}</g>
        <text x={BAR_W / 2} y={BAR_H + 30} textAnchor="middle" fontSize={13} fill="#333">
          总计: {total}
        </text>
      </svg>
    </div>
  );
}
