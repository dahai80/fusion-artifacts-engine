// Issue #63: React VisualEngineDispatcher — 4-level fallback chain as
// specified by the k12-train-model PRD (A8):
//   Level 1: primary component (by meta.visual_type)
//   Level 2: dsl.meta.fallback_type component (tape_diagram | flow_card)
//   Level 3: TapeDiagram (fixed, unless already tried)
//   Level 4: FlowCard (fixed, unless already tried); if it throws too,
//            a static error card renders (100% render guarantee, mirrors
//            visual_dispatcher.py + visual_error_boundary.py).
//
// The chain is a pure function (resolveFallbackChain) so it is testable
// without a DOM: react error boundaries are client-renderer-only and do
// not catch during renderToString.

import type { ComponentType, ReactNode } from "react";
import type { SocraticDsl } from "./types";
import { extractFallbackType, extractVisualType, getComponent } from "./registry";
import { TapeDiagram } from "./primitives/TapeDiagram";
import { FlowCard } from "./primitives/FlowCard";
import { VisualErrorBoundary } from "./ErrorBoundary";
import type { PrimitiveProps } from "./primitives/shared";

export interface VisualEngineDispatcherProps {
  dsl: SocraticDsl | null | undefined;
  progress: number;
}

export function resolveFallbackChain(
  dsl: SocraticDsl | null | undefined,
): Array<ComponentType<PrimitiveProps>> {
  const fallbackType = extractFallbackType(dsl);
  const chain: Array<ComponentType<PrimitiveProps>> = [
    getComponent(extractVisualType(dsl)),
    fallbackType === "flow_card" ? FlowCard : TapeDiagram,
    TapeDiagram,
    FlowCard,
  ];
  // Set dedupes while preserving insertion order — mirrors the Python
  // dispatcher's level semantics (a level already tried is not retried).
  return [...new Set(chain)];
}

function errorCard(err: Error): ReactNode {
  return (
    <div className="fae-primitive fae-render-error" role="alert">
      <p>可视化渲染失败</p>
      <p className="fae-error-detail">{err.message}</p>
    </div>
  );
}

function renderLevel(
  chain: Array<ComponentType<PrimitiveProps>>,
  index: number,
  dsl: SocraticDsl | null | undefined,
  progress: number,
): ReactNode {
  if (index >= chain.length) {
    // Level 4 exhausted — static error card, never throw to the caller.
    return errorCard(new Error("all fallback levels failed"));
  }
  const Level = chain[index];
  return (
    <VisualErrorBoundary fallback={() => renderLevel(chain, index + 1, dsl, progress)}>
      <Level dsl={dsl} progress={progress} />
    </VisualErrorBoundary>
  );
}

export function VisualEngineDispatcher({ dsl, progress }: VisualEngineDispatcherProps) {
  return renderLevel(resolveFallbackChain(dsl), 0, dsl, progress);
}
