// Issue #63: React rendering layer for the Socratic DSL v3.0 visual engine.
// Public surface.

export { VisualEngineDispatcher } from "./VisualEngineDispatcher";
export type { VisualEngineDispatcherProps } from "./VisualEngineDispatcher";
export { VisualStage } from "./VisualStage";
export type { VisualStageProps } from "./VisualStage";
export { useVisualStream } from "./useVisualStream";
export type { UseVisualStreamOptions, VisualStreamState } from "./useVisualStream";
export { VisualErrorBoundary } from "./ErrorBoundary";
export { getComponent, COMPONENT_REGISTRY } from "./registry";
export * from "./types";
export { ArrayGrid } from "./primitives/ArrayGrid";
export { TapeDiagram } from "./primitives/TapeDiagram";
export { Geometry2D } from "./primitives/Geometry2D";
export { Isometric3D } from "./primitives/Isometric3D";
export { TrackTimeline } from "./primitives/TrackTimeline";
export { BucketDivider } from "./primitives/BucketDivider";
export { DataChart } from "./primitives/DataChart";
export { FlowCard } from "./primitives/FlowCard";
