// Port of visual_primitives/bucket_divider.py (issue #63).
// 4-6年级 隔板/抽屉/容斥。元素 fly-in 分配到桶。

import { activeEntities, entitiesOf, parametersOf, titleOf } from "../helpers";
import { COLOR_ACTIVE, COLOR_BASE, type PrimitiveProps } from "./shared";

const BW = 120;
const GAP = 20;
const H = 200;

export function BucketDivider({ dsl, progress }: PrimitiveProps) {
  const params = parametersOf(dsl);
  const entities = entitiesOf(dsl);
  const active = activeEntities(dsl, progress);

  let bucketCount = 0;
  let elementCount = 0;
  for (const [key, val] of Object.entries(params)) {
    if (!val || typeof val !== "object") continue;
    const label = String(val.label ?? key).toLowerCase();
    const v = val.value;
    if (typeof v !== "number" || !Number.isFinite(v)) continue;
    if (label.includes("bucket") || label.includes("桶") || label.includes("group")) {
      bucketCount = Math.max(1, Math.trunc(v));
    } else if (label.includes("element") || label.includes("元素") || label.includes("item")) {
      elementCount = Math.max(0, Math.trunc(v));
    }
  }
  if (bucketCount <= 0) {
    bucketCount = entities.length > 0 ? Math.min(4, Math.max(1, entities.length)) : 3;
  }
  if (elementCount <= 0) elementCount = 8;
  const shown = Math.trunc(elementCount * Math.max(0, Math.min(1, progress)));

  const w = bucketCount * (BW + GAP) + GAP;
  const buckets = [];
  const per = Math.max(1, bucketCount);
  for (let b = 0; b < bucketCount; b++) {
    const bx = GAP + b * (BW + GAP);
    buckets.push(
      <rect
        key={`bucket-${b}`}
        x={bx}
        y={40}
        width={BW}
        height={120}
        rx={8}
        fill="#f8f9fa"
        stroke="#adb5bd"
        strokeWidth={2}
        className="fae-bucket"
        data-id={`bucket_${b}`}
      />,
    );
    const inBucket = Math.trunc(shown / per) + (b < shown % per ? 1 : 0);
    for (let e = 0; e < inBucket; e++) {
      const ex = bx + 20 + (e % 4) * 25;
      const ey = 60 + Math.trunc(e / 4) * 25;
      buckets.push(
        <circle
          key={`bucket-${b}-item-${e}`}
          cx={ex}
          cy={ey}
          r={9}
          fill={active.has(`bucket_${b}`) ? COLOR_ACTIVE : COLOR_BASE}
          className="fae-item"
        />,
      );
    }
    buckets.push(
      <text key={`bucket-${b}-label`} x={bx + BW / 2} y={180} textAnchor="middle" fontSize={13} fill="#495057">
        桶 {b + 1}
      </text>,
    );
  }

  return (
    <div className="fae-primitive fae-bucket-divider">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={w} height={H} viewBox={`0 0 ${w} ${H}`} role="img">
        <text x={w / 2} y={20} textAnchor="middle" fontSize={13} fill="#333">
          {shown}/{elementCount} 个元素分配到 {bucketCount} 桶
        </text>
        {buckets}
      </svg>
    </div>
  );
}
