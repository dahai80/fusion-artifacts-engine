// Port of visual_primitives/base.py helper semantics (issue #63).
// Defensive against malformed DSL — same contract as the Python side:
// any shape of input must render, never throw.

import type { DslStep, DslEntity, SocraticDsl } from "./types";

export function clamp01(p: number): number {
  return Math.max(0, Math.min(1, Number.isFinite(p) ? p : 0));
}

export function metaOf(dsl: SocraticDsl | null | undefined): Partial<SocraticDsl["meta"]> {
  return dsl && typeof dsl === "object" && dsl.meta ? dsl.meta : {};
}

export function parametersOf(dsl: SocraticDsl | null | undefined): SocraticDsl["parameters"] {
  if (dsl && typeof dsl === "object" && dsl.parameters && typeof dsl.parameters === "object") {
    return dsl.parameters;
  }
  return {};
}

export function entitiesOf(dsl: SocraticDsl | null | undefined): DslEntity[] {
  if (dsl && typeof dsl === "object" && Array.isArray(dsl.entities)) {
    return dsl.entities;
  }
  return [];
}

export function pipelineOf(dsl: SocraticDsl | null | undefined): DslStep[] {
  if (dsl && typeof dsl === "object" && Array.isArray(dsl.pipeline)) {
    return dsl.pipeline;
  }
  return [];
}

export function titleOf(dsl: SocraticDsl | null | undefined): string {
  const meta = metaOf(dsl);
  return (meta.id || meta.topic || "visual") as string;
}

// base.py _active_step: first pipeline step whose progress_range covers p.
export function activeStep(dsl: SocraticDsl | null | undefined, progress: number): DslStep | null {
  const pipeline = pipelineOf(dsl);
  if (pipeline.length === 0) {
    return null;
  }
  const p = clamp01(progress);
  for (const step of pipeline) {
    const pr = step?.visual_state?.progress_range;
    if (Array.isArray(pr) && pr.length === 2 && pr[0] <= p && p <= pr[1]) {
      return step;
    }
  }
  return pipeline[pipeline.length - 1];
}

// base.py _active_entities: entity ids highlighted at this progress.
export function activeEntities(
  dsl: SocraticDsl | null | undefined,
  progress: number,
): Set<string> {
  const step = activeStep(dsl, progress);
  const ae = step?.visual_state?.active_entities;
  return new Set(Array.isArray(ae) ? ae : []);
}
