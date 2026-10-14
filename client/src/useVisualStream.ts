// Issue #63: consume the render-visual SSE stream
// (GET /api/v1/render-visual/stream?dsl=...|raw_text=..., #59).
//
// Implemented with fetch + ReadableStream instead of EventSource so the
// X-API-Key header can be sent (EventSource cannot set headers). The
// stream is finite: the server closes it after render_final (#62).

import { useCallback, useEffect, useRef, useState } from "react";
import type { RenderFinalEvent, RenderStepEvent, StreamEvent } from "./types";

export interface UseVisualStreamOptions {
  /** Query params for the stream endpoint: `dsl=<json>` or `raw_text=<text>`. */
  query: string;
  /** Base URL of the fusion-artifacts-engine daemon. Default http://127.0.0.1:11451 */
  baseUrl?: string;
  /** Value for the X-API-Key auth header. */
  apiKey?: string;
  /** When false, no request is made. Default true. */
  enabled?: boolean;
}

export interface VisualStreamState {
  /** Per-step rendered snapshots in arrival order. */
  steps: RenderStepEvent[];
  /** Final full-render event, present after the stream completes. */
  final: RenderFinalEvent | null;
  /** Server-reported render error, if any. */
  error: string | null;
  /** True while the SSE stream is open. */
  streaming: boolean;
}

interface ParsedFrame {
  event: string;
  data: string;
}

export function parseSseChunk(chunk: string): ParsedFrame[] {
  const frames: ParsedFrame[] = [];
  const blocks = chunk.split("\n\n");
  for (const block of blocks) {
    let event = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (data) frames.push({ event, data });
  }
  return frames;
}

export function useVisualStream(options: UseVisualStreamOptions): VisualStreamState {
  const { query, baseUrl = "http://127.0.0.1:11451", apiKey, enabled = true } = options;
  const [steps, setSteps] = useState<RenderStepEvent[]>([]);
  const [final, setFinal] = useState<RenderFinalEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setSteps([]);
    setFinal(null);
    setError(null);
    setStreaming(true);
    try {
      const url = `${baseUrl}/api/v1/render-visual/stream?${query}`;
      const res = await fetch(url, {
        headers: apiKey ? { "X-API-Key": apiKey } : undefined,
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`render-visual stream failed: HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const idx = buffer.lastIndexOf("\n\n");
        if (idx === -1) continue;
        const ready = buffer.slice(0, idx + 2);
        buffer = buffer.slice(idx + 2);
        for (const frame of parseSseChunk(ready)) {
          let payload: unknown;
          try {
            payload = JSON.parse(frame.data);
          } catch {
            continue;
          }
          const evt = { kind: frame.event, data: payload } as StreamEvent;
          if (evt.kind === "render_step") {
            setSteps((prev) => [...prev, evt.data as RenderStepEvent]);
          } else if (evt.kind === "render_final") {
            setFinal(evt.data as RenderFinalEvent);
          } else if (evt.kind === "render_error") {
            setError(String((evt.data as { error?: unknown }).error ?? "unknown error"));
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setError((e as Error).message);
      }
    } finally {
      setStreaming(false);
    }
  }, [baseUrl, query, apiKey]);

  useEffect(() => {
    if (!enabled || !query) return;
    void run();
    return () => abortRef.current?.abort();
  }, [enabled, query, run]);

  return { steps, final, error, streaming };
}
