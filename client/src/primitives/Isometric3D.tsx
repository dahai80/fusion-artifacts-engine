// Port of visual_primitives/isometric_3d.py (issue #63).
// 5-6年级 立体几何/切削展开。
//
// Deviation from the Python compiler: the server side renders via Three.js
// (CDN <script>); the React layer uses a pure-SVG isometric projection so
// the component stays dependency-free and SSR-safe. Visual semantics
// (box per entity, active highlight, spin-free static view) are preserved.

import { activeEntities, entitiesOf, titleOf } from "../helpers";
import { type PrimitiveProps } from "./shared";

const W = 600;
const H = 400;

// 2D screen offset for a 3D point (x, y, z) under an isometric projection.
function iso(x: number, y: number, z: number): [number, number] {
  const sx = (x - y) * Math.cos(Math.PI / 6);
  const sy = (x + y) * Math.sin(Math.PI / 6) - z;
  return [sx, sy];
}

function hex(n: number): string {
  return `#${n.toString(16).padStart(6, "0")}`;
}

export function Isometric3D({ dsl, progress }: PrimitiveProps) {
  const entities = entitiesOf(dsl);
  const active = activeEntities(dsl, progress);

  const boxes =
    entities.length > 0
      ? entities.map((ent, i) => {
          const props = (ent.properties ?? {}) as Record<string, unknown>;
          const num = (k: string, dflt: number): number => {
            const v = props[k];
            return typeof v === "number" && Number.isFinite(v) ? v : dflt;
          };
          const colorStr = String(props.color ?? "");
          let color = 0x4dabf7;
          if (colorStr.includes("red")) color = 0xff6b6b;
          else if (colorStr.includes("green")) color = 0x69db7c;
          return {
            id: String(ent.id ?? `ent_${i}`),
            w: num("width", num("w", 2)),
            h: num("height", num("h", 2)),
            d: num("depth", num("d", 2)),
            color: hex(color),
            opacity: active.has(String(ent.id ?? `ent_${i}`)) ? 0.85 : 0.5,
            slot: i,
          };
        })
      : [{ id: "box", w: 2, h: 2, d: 2, color: hex(0x4dabf7), opacity: 0.85, slot: 0 }];

  const scale = 24;
  const faces: React.ReactNode[] = [];
  for (const b of boxes) {
    const ox = ((b.slot % 3) - 1) * 5;
    const p = (x: number, y: number, z: number): [number, number] => {
      const [sx, sy] = iso(x, y, z);
      return [W / 2 + (sx + ox) * scale, H / 2 + sy * scale + 20];
    };
    // Top face (z = h), front-left (y = d), front-right (x = w).
    const t = [p(0, 0, b.h), p(b.w, 0, b.h), p(b.w, b.d, b.h), p(0, b.d, b.h)];
    const left = [p(0, b.d, b.h), p(b.w, b.d, b.h), p(b.w, b.d, 0), p(0, b.d, 0)];
    const right = [p(b.w, 0, b.h), p(b.w, b.d, b.h), p(b.w, b.d, 0), p(b.w, 0, 0)];
    const pts = (quad: Array<[number, number]>): string =>
      quad.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    faces.push(
      <g key={b.id} className="fae-iso-box" data-id={b.id} opacity={b.opacity}>
        <polygon points={pts(left)} fill={b.color} stroke="#333" strokeWidth={1} />
        <polygon points={pts(right)} fill={b.color} stroke="#333" strokeWidth={1} filter="brightness(0.85)" />
        <polygon points={pts(t)} fill={b.color} stroke="#333" strokeWidth={1} filter="brightness(1.15)" />
      </g>,
    );
  }

  return (
    <div className="fae-primitive fae-isometric-3d">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img">
        {faces}
      </svg>
    </div>
  );
}
