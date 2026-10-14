// Issue #63: visual_type → React component registry.
// Mirrors visual_primitives/__init__.py COMPILER_REGISTRY; unknown types
// fall back to FlowCard (same as the Python get_compiler).

import type { ComponentType } from "react";
import type { SocraticDsl, VisualType } from "./types";
import { ArrayGrid } from "./primitives/ArrayGrid";
import { TapeDiagram } from "./primitives/TapeDiagram";
import { Geometry2D } from "./primitives/Geometry2D";
import { Isometric3D } from "./primitives/Isometric3D";
import { TrackTimeline } from "./primitives/TrackTimeline";
import { BucketDivider } from "./primitives/BucketDivider";
import { DataChart } from "./primitives/DataChart";
import { FlowCard } from "./primitives/FlowCard";
import type { PrimitiveProps } from "./primitives/shared";

export const COMPONENT_REGISTRY: Record<VisualType, ComponentType<PrimitiveProps>> = {
  array_grid: ArrayGrid,
  tape_diagram: TapeDiagram,
  geometry_2d: Geometry2D,
  isometric_3d: Isometric3D,
  track_timeline: TrackTimeline,
  bucket_divider: BucketDivider,
  data_chart: DataChart,
  flow_card: FlowCard,
};

export function getComponent(visualType: string): ComponentType<PrimitiveProps> {
  return COMPONENT_REGISTRY[visualType as VisualType] ?? FlowCard;
}

export function extractVisualType(dsl: SocraticDsl | null | undefined): string {
  const vt = dsl?.meta?.visual_type;
  return typeof vt === "string" ? vt : "flow_card";
}

export function extractFallbackType(dsl: SocraticDsl | null | undefined): string {
  const ft = dsl?.meta?.fallback_type;
  return ft === "tape_diagram" || ft === "flow_card" ? ft : "tape_diagram";
}
