// Port of visual_primitives/array_grid.py (issue #63).
// 1-2年级 计数/阵列/乘法。点阵/阵列网格，active 高亮为红。

import { activeEntities, parametersOf, titleOf } from "../helpers";
import { COLOR_ACTIVE, COLOR_BASE, type PrimitiveProps } from "./shared";

const CELL = 44;
const GAP = 6;

export function ArrayGrid({ dsl, progress }: PrimitiveProps) {
  const params = parametersOf(dsl);
  let count = 0;
  let rows = 1;
  let cols = 1;
  for (const [key, val] of Object.entries(params)) {
    if (!val || typeof val !== "object") continue;
    const label = String(val.label ?? key).toLowerCase();
    const v = val.value;
    if (typeof v !== "number" || !Number.isFinite(v)) continue;
    if (label.includes("count") || label.includes("总数") || label.includes("total")) {
      count = Math.trunc(v);
    } else if (label.includes("row") || label.includes("行")) {
      rows = Math.max(1, Math.trunc(v));
    } else if (label.includes("col") || label.includes("列")) {
      cols = Math.max(1, Math.trunc(v));
    }
  }
  if (count <= 0) count = rows * cols > 0 ? rows * cols : 12;
  if (rows * cols <= 0) {
    rows = 1;
    cols = count;
  }
  const total = rows * cols;
  const shown = Math.min(count, total);
  const active = activeEntities(dsl, progress);

  const w = cols * (CELL + GAP) + GAP;
  const h = rows * (CELL + GAP) + GAP + 40;
  const dots = [];
  let idx = 0;
  for (let r = 0; r < rows && idx < shown; r++) {
    for (let c = 0; c < cols && idx < shown; c++) {
      const cx = GAP + c * (CELL + GAP) + CELL / 2;
      const cy = GAP + r * (CELL + GAP) + CELL / 2;
      const entId = `dot_${idx}`;
      dots.push(
        <circle
          key={entId}
          cx={cx}
          cy={cy}
          r={16}
          fill={active.has(entId) ? COLOR_ACTIVE : COLOR_BASE}
          className="fae-dot"
          data-id={entId}
        />,
      );
      idx++;
    }
  }

  return (
    <div className="fae-primitive fae-array-grid">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img">
        {dots}
        <text x={w / 2} y={h - 8} textAnchor="middle" fontSize={14} fill="#333">
          共 {shown} 个
        </text>
      </svg>
    </div>
  );
}
