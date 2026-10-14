// Shared primitive props + palette. Each primitive mirrors the visual
// semantics of its Python compiler in fusion_artifacts_engine/visual_primitives/.

import type { SocraticDsl } from "../types";

export interface PrimitiveProps {
  dsl: SocraticDsl | null | undefined;
  progress: number;
}

export const PALETTE = ["#4dabf7", "#69db7c", "#ffd43b", "#ff6b6b", "#da77f2", "#ffa94d"];

export const COLOR_ACTIVE = "#ff6b6b";
export const COLOR_BASE = "#4dabf7";
