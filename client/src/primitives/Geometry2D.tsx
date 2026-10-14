// Port of visual_primitives/geometry_2d.py (issue #63).
// 3-5年级 周长/面积/拼剪。2D 画布：rect/circle/triangle。

import type { ReactNode } from "react";
import { activeEntities, entitiesOf, titleOf } from "../helpers";
import { PALETTE, type PrimitiveProps } from "./shared";

const W = 600;
const H = 400;

export function Geometry2D({ dsl, progress }: PrimitiveProps) {
  const entities = entitiesOf(dsl);
  const active = activeEntities(dsl, progress);
  const shapes: ReactNode[] = [];

  entities.forEach((ent, i) => {
    if (!ent || typeof ent !== "object") return;
    const eid = String(ent.id ?? `ent_${i}`);
    const etype = String(ent.type ?? "rect").toLowerCase();
    const props = (ent.properties ?? {}) as Record<string, unknown>;
    const num = (k: string, dflt: number): number => {
      const v = props[k];
      return typeof v === "number" && Number.isFinite(v) ? v : dflt;
    };
    const color = PALETTE[i % PALETTE.length];
    const isActive = active.has(eid);
    const opacity = isActive ? 0.85 : 0.5;
    const stroke = isActive ? "#333" : "#999";
    const strokeWidth = isActive ? 3 : 1;
    const common = {
      fill: color,
      fillOpacity: opacity,
      stroke,
      strokeWidth,
      className: "fae-shape",
      "data-id": eid,
    };

    if (etype.includes("rect") || etype.includes("square")) {
      shapes.push(
        <rect
          key={eid}
          {...common}
          x={num("x", 50 + i * 80)}
          y={num("y", 50)}
          width={num("width", 80)}
          height={num("height", 60)}
        />,
      );
    } else if (etype.includes("circle")) {
      shapes.push(
        <circle key={eid} {...common} cx={num("cx", 100 + i * 80)} cy={num("cy", 100)} r={num("r", 40)} />,
      );
    } else if (etype.includes("triangle") || etype.includes("polygon")) {
      const pts = props.points ? String(props.points) : "50,200 150,100 250,200";
      shapes.push(<polygon key={eid} {...common} points={pts} />);
    } else {
      shapes.push(
        <rect
          key={eid}
          {...common}
          x={num("x", 50 + i * 80)}
          y={num("y", 50)}
          width={num("width", 80)}
          height={num("height", 60)}
        />,
      );
    }
  });

  if (shapes.length === 0) {
    shapes.push(
      <rect key="placeholder" x={50} y={50} width={200} height={120} fill="#4dabf7" fillOpacity={0.5} stroke="#999" strokeWidth={1} />,
    );
  }

  return (
    <div className="fae-primitive fae-geometry-2d">
      <h2 className="fae-title">{titleOf(dsl)}</h2>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img">
        {shapes}
      </svg>
    </div>
  );
}
