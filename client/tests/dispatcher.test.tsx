// Issue #63: VisualEngineDispatcher component selection + fallback chain.
//
// react error boundaries do not catch during renderToString (they are a
// client-renderer feature), so the chain ORDER is asserted via the pure
// resolveFallbackChain helper; SSR rendering covers the non-throwing paths.

import { describe, expect, test, afterEach } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import {
  VisualEngineDispatcher,
  resolveFallbackChain,
} from "../src/VisualEngineDispatcher";
import { COMPONENT_REGISTRY, getComponent } from "../src/registry";
import { TapeDiagram } from "../src/primitives/TapeDiagram";
import { FlowCard } from "../src/primitives/FlowCard";
import { makeDsl } from "./fixtures";
import type { VisualType } from "../src/types";

function ThrowingPrimary(): never {
  throw new Error("primary render failed");
}

describe("VisualEngineDispatcher", () => {
  const originalArrayGrid = COMPONENT_REGISTRY["array_grid"];
  const originalTape = COMPONENT_REGISTRY["tape_diagram"];

  afterEach(() => {
    COMPONENT_REGISTRY["array_grid"] = originalArrayGrid;
    COMPONENT_REGISTRY["tape_diagram"] = originalTape;
  });

  test("renders every visual_type from valid DSL", () => {
    for (const vt of [
      "array_grid",
      "tape_diagram",
      "geometry_2d",
      "isometric_3d",
      "track_timeline",
      "bucket_divider",
      "data_chart",
      "flow_card",
    ] as VisualType[]) {
      const html = renderToStaticMarkup(
        <VisualEngineDispatcher dsl={makeDsl(vt)} progress={0.5} />,
      );
      expect(html.length).toBeGreaterThan(100);
    }
  });

  test("unknown visual_type resolves to FlowCard first", () => {
    const dsl = makeDsl("array_grid");
    (dsl.meta as any).visual_type = "no_such_type";
    const chain = resolveFallbackChain(dsl);
    expect(chain[0]).toBe(getComponent("flow_card"));
  });

  test("chain: primary → tape_diagram (default fallback) → flow_card", () => {
    const chain = resolveFallbackChain(makeDsl("array_grid"));
    expect(chain[0]).toBe(COMPONENT_REGISTRY["array_grid"]);
    expect(chain[1]).toBe(TapeDiagram);
    expect(chain[2]).toBe(FlowCard);
    expect(chain.length).toBe(3);
  });

  test("chain: fallback_type=flow_card overrides level 2", () => {
    const chain = resolveFallbackChain(makeDsl("array_grid", "flow_card"));
    expect(chain[0]).toBe(COMPONENT_REGISTRY["array_grid"]);
    expect(chain[1]).toBe(FlowCard);
    expect(chain[2]).toBe(TapeDiagram);
  });

  test("chain: primary == tape_diagram dedupes level 2", () => {
    const chain = resolveFallbackChain(makeDsl("tape_diagram"));
    expect(chain[0]).toBe(TapeDiagram);
    expect(chain[1]).toBe(FlowCard);
    expect(chain.length).toBe(2);
  });

  test("rendered output reflects the primary when it succeeds", () => {
    const dsl = makeDsl("tape_diagram");
    const html = renderToStaticMarkup(
      <VisualEngineDispatcher dsl={dsl} progress={0.5} />,
    );
    expect(html).toContain("fae-tape-diagram");
  });
});
