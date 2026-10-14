// Shared test fixture: a valid Socratic DSL v3.0 document (schema-conformant
// per fusion_core/dsl_schema.py).

import type { SocraticDsl, VisualType } from "../src/types";

export function makeDsl(visualType: VisualType, fallbackType: "tape_diagram" | "flow_card" = "tape_diagram"): SocraticDsl {
  return {
    meta: { id: "prob-001", grade: 3, topic: "行程问题", visual_type: visualType, fallback_type: fallbackType },
    parameters: {
      total: { label: "总数", value: 12, unit: "个" },
      rows: { label: "行", value: 3, unit: "行" },
      cols: { label: "列", value: 4, unit: "列" },
    },
    entities: [
      { id: "a", type: "runner", label: "甲", properties: { speed: 60, start: 0 } },
      { id: "b", type: "runner", label: "乙", properties: { speed: 40, start: 0 } },
    ],
    pipeline: [
      {
        step: 1,
        title: "求速度和",
        formula: "60 + 40",
        eval: "100",
        result_unit: "米/分",
        visual_state: { progress_range: [0.0, 0.5], active_entities: ["a", "b"], action: "combine" },
      },
      {
        step: 2,
        title: "求相遇时间",
        formula: "600 / 100",
        eval: "6",
        result_unit: "分",
        visual_state: { progress_range: [0.5, 1.0], active_entities: ["a", "b"], action: "divide" },
      },
    ],
  };
}

export const SAMPLE_RENDER_STEP_HTML = "<div class='flow-container'><p>html from server</p></div>";
