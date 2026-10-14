// Issue #63: all 8 primitives render valid and malformed DSL without
// throwing (mirrors the Python "100% render guarantee" contract).

import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ArrayGrid,
  BucketDivider,
  DataChart,
  FlowCard,
  Geometry2D,
  Isometric3D,
  TapeDiagram,
  TrackTimeline,
} from "../src/primitives";
import { makeDsl } from "./fixtures";
import type { VisualType } from "../src/types";

const ALL_TYPES: VisualType[] = [
  "array_grid",
  "tape_diagram",
  "geometry_2d",
  "isometric_3d",
  "track_timeline",
  "bucket_divider",
  "data_chart",
  "flow_card",
];

const RENDERERS: Record<VisualType, (dsl: any, p: number) => string> = {
  array_grid: (d, p) => renderToStaticMarkup(<ArrayGrid dsl={d} progress={p} />),
  tape_diagram: (d, p) => renderToStaticMarkup(<TapeDiagram dsl={d} progress={p} />),
  geometry_2d: (d, p) => renderToStaticMarkup(<Geometry2D dsl={d} progress={p} />),
  isometric_3d: (d, p) => renderToStaticMarkup(<Isometric3D dsl={d} progress={p} />),
  track_timeline: (d, p) => renderToStaticMarkup(<TrackTimeline dsl={d} progress={p} />),
  bucket_divider: (d, p) => renderToStaticMarkup(<BucketDivider dsl={d} progress={p} />),
  data_chart: (d, p) => renderToStaticMarkup(<DataChart dsl={d} progress={p} />),
  flow_card: (d, p) => renderToStaticMarkup(<FlowCard dsl={d} progress={p} />),
};

describe("primitives", () => {
  test("every visual_type renders valid DSL at multiple progress points", () => {
    for (const vt of ALL_TYPES) {
      for (const p of [0, 0.25, 0.6, 1]) {
        const html = RENDERERS[vt](makeDsl(vt), p);
        // flow_card renders step-card divs; the other 7 render SVG.
        if (vt === "flow_card") {
          expect(html).toContain("fae-flow-container");
        } else {
          expect(html).toContain("<svg");
        }
        expect(html.length).toBeGreaterThan(100);
      }
    }
  });

  test("every primitive renders malformed DSL without throwing", () => {
    // Same defensive contract as the Python compilers: null / wrong-shape
    // input must still produce output.
    for (const vt of ALL_TYPES) {
      const html = RENDERERS[vt](null as any, 0.5);
      expect(html.length).toBeGreaterThan(0);
      const html2 = RENDERERS[vt]({ meta: {} } as any, 0.5);
      expect(html2.length).toBeGreaterThan(0);
    }
  });

  test("array_grid highlights active entities (progress-driven)", () => {
    // dot_N entity ids are never in active_entities; but a DSL with a
    // pipeline step marking dot_0 active must turn it red.
    const dsl = makeDsl("array_grid");
    dsl.pipeline[0].visual_state.active_entities = ["dot_0"];
    const html = renderToStaticMarkup(<ArrayGrid dsl={dsl} progress={0.25} />);
    expect(html).toContain("#ff6b6b");
  });

  test("tape_diagram falls back to A/B segments on empty params", () => {
    const dsl = makeDsl("tape_diagram");
    dsl.parameters = {};
    const html = renderToStaticMarkup(<TapeDiagram dsl={dsl} progress={0.5} />);
    expect(html).toContain("A");
    expect(html).toContain("B");
  });

  test("flow_card hides future steps (progress < range start)", () => {
    const dsl = makeDsl("flow_card");
    const html = renderToStaticMarkup(<FlowCard dsl={dsl} progress={0.2} />);
    expect(html).toContain("fae-active");
    expect(html).toContain("fae-hidden");
  });

  test("flow_card renders placeholder on empty pipeline", () => {
    const dsl = makeDsl("flow_card");
    dsl.pipeline = [];
    const html = renderToStaticMarkup(<FlowCard dsl={dsl} progress={0.5} />);
    expect(html).toContain("暂无解题步骤数据");
  });

  test("track_timeline uses default runners when no entities", () => {
    const dsl = makeDsl("track_timeline");
    dsl.entities = [];
    const html = renderToStaticMarkup(<TrackTimeline dsl={dsl} progress={0.5} />);
    expect(html).toContain("甲");
    expect(html).toContain("乙");
  });
});
