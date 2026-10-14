// Port of visual_primitives/track_timeline.py (issue #63).
// 4-6年级 行程/追及/流水。多体匀速运动 / 位置追踪。

import { activeEntities, entitiesOf, titleOf } from "../helpers";
import { PALETTE, type PrimitiveProps } from "./shared";

const TRACK_LEN = 500;
const TRACK_H = 200;
const LANE_H = 40;

interface Runner {
  id: string;
  label: string;
  speed: number;
  start: number;
}

export function TrackTimeline({ dsl, progress }: PrimitiveProps) {
  const entities = entitiesOf(dsl);
  const active = activeEntities(dsl, progress);

  const runners: Runner[] = [];
  const src =
    entities.length > 0
      ? entities
      : [
          { id: "a", label: "甲", properties: { speed: 60 } },
          { id: "b", label: "乙", properties: { speed: 40 } },
        ];
  src.forEach((ent, i) => {
    if (!ent || typeof ent !== "object") return;
    const props = (ent.properties ?? {}) as Record<string, unknown>;
    const num = (k: string, dflt: number): number => {
      const v = props[k];
      return typeof v === "number" && Number.isFinite(v) ? v : dflt;
    };
    runners.push({
      id: String(ent.id ?? `ent_${i}`),
      label: String(ent.label ?? ent.id ?? `ent_${i}`),
      speed: num("speed", num("v", 10)),
      start: num("start", 0),
    });
  });

  const lanes = runners.map((r, i) => {
    const pos = Math.min(r.start + r.speed * progress * 3, TRACK_LEN - 20);
    const y = i * LANE_H + 20;
    const color = PALETTE[i % PALETTE.length];
    const opacity = active.has(r.id) ? 1.0 : 0.6;
    return (
      <g key={r.id} className="fae-runner" data-id={r.id}>
        <line x1={10} y1={y} x2={TRACK_LEN} y2={y} stroke="#ddd" strokeWidth={2} />
        <circle cx={pos + 10} cy={y} r={12} fill={color} opacity={opacity} />
        <text x={pos + 10} y={y - 16} textAnchor="middle" fontSize={12} fill="#333">
          {r.label}
        </text>
      </g>
    );
  });

  return (
    <div className="fae-primitive fae-track-timeline">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={TRACK_LEN + 20} height={TRACK_H} viewBox={`0 0 ${TRACK_LEN + 20} ${TRACK_H}`} role="img">
        {lanes}
        <text x={TRACK_LEN / 2} y={TRACK_H - 5} textAnchor="middle" fontSize={12} fill="#666">
          progress: {progress.toFixed(1)}
        </text>
      </svg>
    </div>
  );
}
