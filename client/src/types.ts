// Issue #63: shared types for the Socratic DSL v3.0 React rendering layer.
// Shape mirrors fusion_core/dsl_schema.py (SOCRATIC_DSL_V3_SCHEMA).

export const VISUAL_TYPES = [
  "array_grid",
  "tape_diagram",
  "geometry_2d",
  "isometric_3d",
  "track_timeline",
  "bucket_divider",
  "data_chart",
  "flow_card",
] as const;

export type VisualType = (typeof VISUAL_TYPES)[number];

export type FallbackType = "tape_diagram" | "flow_card";

export interface DslMeta {
  id: string;
  grade: number;
  topic: string;
  visual_type: VisualType;
  fallback_type: FallbackType;
}

export interface DslParameter {
  label: string;
  value: number;
  unit: string;
}

export interface DslEntity {
  id: string;
  type: string;
  label: string;
  properties: Record<string, unknown>;
}

export interface DslVisualState {
  progress_range: [number, number];
  active_entities: string[];
  action: string;
}

export interface DslStep {
  step: number;
  title: string;
  formula: string;
  eval: string;
  result_unit: string;
  visual_state: DslVisualState;
}

export interface SocraticDsl {
  meta: DslMeta;
  parameters: Record<string, DslParameter>;
  entities: DslEntity[];
  pipeline: DslStep[];
}

// SSE stream events from GET /api/v1/render-visual/stream (#59).
export interface RenderStepEvent {
  step: number;
  progress: number;
  html: string;
}

export interface RenderFinalEvent {
  step: "final";
  progress: number;
  html: string;
  visual_type: VisualType;
}

export type StreamEvent =
  | { kind: "render_step"; data: RenderStepEvent }
  | { kind: "render_final"; data: RenderFinalEvent }
  | { kind: "render_error"; data: { error: string } };
