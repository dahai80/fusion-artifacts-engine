import { describe, expect, test } from "bun:test";
import { parseSseChunk } from "../src/useVisualStream";

const STEP_FRAME = [
  "event: render_step",
  'data: {"step":0,"progress":0.25,"html":"<p>a</p>"}',
  "",
  "",
].join("\n");

const FINAL_FRAME = [
  "event: render_final",
  'data: {"step":"final","progress":1.0,"html":"<p>f</p>"}',
  "",
  "",
].join("\n");

describe("parseSseChunk", () => {
  test("parses render_step and render_final frames", () => {
    const frames = parseSseChunk(STEP_FRAME + FINAL_FRAME);
    expect(frames.length).toBe(2);
    expect(frames[0].event).toBe("render_step");
    expect(frames[0].data).toContain('"step":0');
    expect(frames[1].event).toBe("render_final");
    expect(frames[1].data).toContain('"progress":1.0');
  });

  test("ignores frames without data", () => {
    const frames = parseSseChunk("event: ping\n\n" + STEP_FRAME);
    expect(frames.length).toBe(1);
    expect(frames[0].event).toBe("render_step");
  });

  test("returns empty for empty chunk", () => {
    expect(parseSseChunk("").length).toBe(0);
  });
});
