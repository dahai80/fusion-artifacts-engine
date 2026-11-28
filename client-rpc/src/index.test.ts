import { describe, expect, test } from "bun:test";

import { ArtifactsClient, ErrorCodes, RpcError } from "./index";

// contract/rpc-surface.json is generated from the Python handler
// (ci/gen_rpc_surface.py) and is the single source of truth for both repos.
const contract = JSON.parse(
  await Bun.file(new URL("../../contract/rpc-surface.json", import.meta.url)).text(),
) as { methods: string[]; count: number };

// Surface methods intentionally not wrapped by typed helpers; reachable via
// the generic client.call(). Must be exactly surface-minus-wrapped: a newly
// added engine method that is neither wrapped nor listed here fails CI, and
// so does a stale entry for a removed method.
const UNWRAPPED_ALLOWLIST: ReadonlySet<string> = new Set([
  "artifact.export",
  "artifact.export_session",
  "artifact.import",
  "artifact.export_code",
  "artifact.import_code",
  "artifact.watch",
  "artifact.list_all",
  "artifact.create_folder",
  "artifact.list_folders",
  "artifact.rename_folder",
  "artifact.delete_folder",
  "artifact.move_to_folder",
  "artifact.add_tag",
  "artifact.remove_tag",
  "artifact.list_tags",
  "artifact.list_artifact_tags",
  "artifact.emit_event",
  "artifact.workflow.create",
  "artifact.workflow.revision",
  "artifact.comment",
  "artifact.list_comments",
  "artifact.list_events",
  "artifact.move_to_project_kb",
  "artifact.create_external",
  "artifact.list_by_source",
  "artifact.search_library",
  "artifact.retry_archive",
  "artifact.toc",
  "artifact.view",
  "artifact.auto_compact",
  "artifact.version_diff",
  "artifact.render",
  "artifact.check_safety",
  "artifact.inject",
  "artifact.interact",
  "artifact.sync",
  "render.visual",
  "ai.generate",
  "ai.list_models",
  "context.budget",
]);

// One entry per typed helper, in the exact order runAllHelpers invokes them.
const WRAPPED_METHODS: readonly string[] = [
  "ping",
  "artifact.create",
  "artifact.get",
  "artifact.get_content",
  "artifact.list",
  "artifact.update",
  "artifact.patch",
  "artifact.load",
  "artifact.delete",
  "artifact.version_list",
  "artifact.version_rollback",
  "artifact.rename",
  "artifact.star",
  "artifact.pin",
  "artifact.duplicate",
  "artifact.remix",
  "artifact.list_recycle",
  "artifact.restore",
  "artifact.purge_expired",
  "artifact.create_share",
  "artifact.get_shared",
  "artifact.revoke_share",
  "artifact.create_snapshot",
  "artifact.list_snapshots",
];

describe("contract completeness (issue #141)", () => {
  test("wrapped + allow-listed exactly covers the surface, no gaps, no phantoms", () => {
    const surface = new Set(contract.methods);
    const covered = new Set([...WRAPPED_METHODS, ...UNWRAPPED_ALLOWLIST]);
    const missing = [...surface].filter((m) => !covered.has(m));
    const phantom = [...covered].filter((m) => !surface.has(m));
    expect(missing).toEqual([]);
    expect(phantom).toEqual([]);
  });

  test("each wrapped helper maps to the intended RPC method", async () => {
    const calls: string[] = [];
    const fetchImpl: typeof fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push((JSON.parse(String(init!.body)) as { method: string }).method);
      return ok({});
    }) as typeof fetch;
    const c = new ArtifactsClient("http://x", { fetch: fetchImpl });
    await runAllHelpers(c);
    expect(calls).toEqual([...WRAPPED_METHODS]);
  });

  test("surface count matches methods list", () => {
    expect(contract.count).toBe(contract.methods.length);
    expect(contract.methods).toContain("artifact.create");
    expect(contract.methods).toContain("ping");
  });
});

describe("transport", () => {
  test("sends JSON-RPC envelope with X-API-Key and unwraps result", async () => {
    let captured: { url: string; init: RequestInit } | null = null;
    const fetchImpl: typeof fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      captured = { url: String(input), init: init as RequestInit };
      return ok({ version: "0.7.21" });
    }) as typeof fetch;
    const client = new ArtifactsClient("http://127.0.0.1:11451", {
      apiKey: "k-test",
      fetch: fetchImpl,
    });
    const result = await client.ping();
    expect(result).toEqual({ version: "0.7.21" });
    const body = JSON.parse(String(captured!.init.body)) as Record<string, unknown>;
    expect(body.jsonrpc).toBe("2.0");
    expect(body.method).toBe("ping");
    expect((captured!.init.headers as Record<string, string>)["X-API-Key"]).toBe("k-test");
    expect(captured!.url).toBe("http://127.0.0.1:11451/");
  });

  test("raises RpcError with the server business code", async () => {
    const client = new ArtifactsClient("http://127.0.0.1:11451", {
      fetch: err(-32001, "Artifact not found: art_x"),
    });
    try {
      await client.getArtifact("art_x");
      throw new Error("expected RpcError");
    } catch (e) {
      expect(e).toBeInstanceOf(RpcError);
      expect((e as RpcError).code).toBe(ErrorCodes.NotFound);
      expect((e as RpcError).message).toContain("art_x");
    }
  });

  test("create passes idempotency_key through", async () => {
    let body: Record<string, unknown> | null = null;
    const fetchImpl: typeof fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      body = JSON.parse(String(init!.body)) as Record<string, unknown>;
      return ok({ artifact: {}, version: {}, ref_text: "[]" });
    }) as typeof fetch;
    const client = new ArtifactsClient("http://127.0.0.1:11451", { fetch: fetchImpl });
    await client.createArtifact({
      session_id: "s1",
      name: "n",
      type: "markdown",
      content: "c",
      idempotency_key: "op-42",
    });
    expect((body!.params as Record<string, unknown>).idempotency_key).toBe("op-42");
  });
});

// ---- helpers ----

function ok(result: unknown): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result }), {
    headers: { "Content-Type": "application/json" },
  });
}

function err(code: number, message: string): typeof fetch {
  return (async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, error: { code, message } }), {
      headers: { "Content-Type": "application/json" },
    })) as unknown as typeof fetch;
}

async function runAllHelpers(c: ArtifactsClient): Promise<void> {
  await c.ping();
  await c.createArtifact({ session_id: "s", name: "n", type: "markdown", content: "c" });
  await c.getArtifact("a");
  await c.getContent("a");
  await c.listArtifacts("s");
  await c.updateArtifact({ artifact_id: "a", content: "c" });
  await c.patchArtifact("a");
  await c.loadArtifact("a");
  await c.deleteArtifact("a");
  await c.listVersions("a");
  await c.rollbackVersion("a", 1);
  await c.renameArtifact("a", "n");
  await c.starArtifact("a");
  await c.pinArtifact("a");
  await c.duplicateArtifact("a");
  await c.remixArtifact("a");
  await c.listRecycle();
  await c.restoreArtifact("a");
  await c.purgeExpired();
  await c.createShare({ artifact_id: "a" });
  await c.getShared("shr");
  await c.revokeShare("shr");
  await c.createSnapshot("a");
  await c.listSnapshots("a");
}
