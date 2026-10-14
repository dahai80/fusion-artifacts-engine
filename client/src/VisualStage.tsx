// Issue #63: top-level React component. Two modes:
//   - Local: pass `dsl` + `progress` directly (component selection +
//     4-level fallback run client-side via VisualEngineDispatcher).
//   - Streamed: pass `rawText` (or `dslJson`) + endpoint auth; the DSL is
//     parsed server-side and per-step renders stream over SSE; by default
//     the streamed HTML is used, but with `useDispatcher` the component
//     re-renders the DSL locally at each streamed progress point.

import { useMemo } from "react";
import { VisualEngineDispatcher } from "./VisualEngineDispatcher";
import { useVisualStream } from "./useVisualStream";
import type { SocraticDsl } from "./types";

export interface VisualStageProps {
  dsl?: SocraticDsl | null;
  /** Raw model output containing <thinking>/<dsl> tags — parsed server-side. */
  rawText?: string;
  progress?: number;
  /** Streamed mode options (required when rawText is given). */
  baseUrl?: string;
  apiKey?: string;
  /** Use the streamed per-step progress to drive the local dispatcher
   *  instead of displaying the server-rendered HTML. Default false. */
  useDispatcher?: boolean;
}

export function VisualStage({
  dsl,
  rawText,
  progress = 1.0,
  baseUrl,
  apiKey,
  useDispatcher: useLocalDispatcher = false,
}: VisualStageProps) {
  const query = useMemo(() => {
    if (rawText != null) return `raw_text=${encodeURIComponent(rawText)}`;
    if (dsl != null) return `dsl=${encodeURIComponent(JSON.stringify(dsl))}`;
    return "";
  }, [rawText, dsl]);

  const stream = useVisualStream({ query, baseUrl, apiKey, enabled: query !== "" });

  if (dsl != null && (rawText == null || useLocalDispatcher)) {
    return <VisualEngineDispatcher dsl={dsl} progress={progress} />;
  }

  // Streamed mode: show the latest server-rendered step HTML.
  const latest = stream.final ?? (stream.steps.length > 0 ? stream.steps[stream.steps.length - 1] : null);
  if (latest != null) {
    return (
      <div
        className="fae-visual-stage fae-streamed"
        dangerouslySetInnerHTML={{ __html: latest.html }}
      />
    );
  }
  if (stream.error) {
    return (
      <div className="fae-visual-stage fae-render-error" role="alert">
        <p>可视化流式渲染失败</p>
        <p className="fae-error-detail">{stream.error}</p>
      </div>
    );
  }
  return (
    <div className="fae-visual-stage fae-loading">
      <p>{stream.streaming ? "渲染中…" : "暂无可视化数据"}</p>
    </div>
  );
}
