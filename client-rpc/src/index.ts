// Official TypeScript JSON-RPC client for fusion-artifacts-engine (issue #141).
// Python (fusion_artifacts_engine/rpc/methods.py) is the source of truth for the
// surface: contract/rpc-surface.json is generated from it, and the test in this
// package asserts every wrapped method exists there and flags surface drift.

export interface ArtifactsClientOptions {
  apiKey?: string;
  fetch?: typeof fetch;
  timeoutMs?: number;
}

export interface JsonRpcResponse<T> {
  jsonrpc: "2.0";
  id: number | string | null;
  result?: T;
  error?: { code: number; message: string; data?: unknown };
}

export interface Artifact {
  id: string;
  session_id: string;
  name: string;
  type: string;
  kind: string;
  project_id: string | null;
  current_version: number;
  summary: string;
  metadata: Record<string, unknown> | null;
  owner_user_id: string | null;
  ownership_type: string;
  created_at: number;
  updated_at: number;
}

export interface ArtifactVersion {
  artifact_id: string;
  version_num: number;
  size_bytes: number;
  token_count: number;
  change_log: string | null;
  source: string | null;
  created_at: number;
}

export interface CreateParams {
  session_id: string;
  name: string;
  type: "code" | "markdown" | "html" | "react" | "svg" | "mermaid" | "data";
  content: string;
  summary?: string;
  change_log?: string;
  kind?: "app" | "code" | "document" | "game" | "tool" | "template";
  project_id?: string;
  metadata?: Record<string, unknown>;
  owner_user_id?: string;
  ownership_type?: string;
  // #141: retry-safe create — replay with the same key within the same
  // session/project returns the original artifact instead of duplicating.
  idempotency_key?: string;
}

export interface UpdateParams {
  artifact_id: string;
  content: string;
  change_log?: string;
  source?: "manual" | "ai_generation";
  // optimistic locking: create fails with a conflict when the stored hash
  // differs, so a retried update can never clobber a concurrent write
  expected_content_hash?: string;
}

export interface CreateShareParams {
  artifact_id: string;
  created_by?: string;
  expires_at?: number;
  max_accesses?: number;
  permission?: "view" | "remix";
}

export class RpcError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "RpcError";
    this.code = code;
    this.data = data;
  }
}

// Business codes (rpc/errors.py): callers branch on code for retry-vs-abandon.
export const ErrorCodes = {
  Parse: -32700,
  InvalidRequest: -32600,
  MethodNotFound: -32601,
  InvalidParams: -32602,
  Internal: -32603,
  NotFound: -32001,
  Conflict: -32002,
  ResourceLimit: -32003,
  BusinessRule: -32004,
  NotImplemented: -32005,
  Permission: -32006,
} as const;

// Retry policy (docs/acl-semantics.md): read-only methods are always safe to
// retry; artifact.create is retry-safe when an idempotency_key is sent;
// artifact.update is retry-safe when expected_content_hash is sent; every
// other mutating method must only be retried after checking current state.
export const RETRY_SAFE_READ = [
  "ping",
  "artifact.get",
  "artifact.get_content",
  "artifact.list",
  "artifact.list_all",
  "artifact.list_recycle",
  "artifact.list_snapshots",
  "artifact.get_shared",
  "artifact.load",
  "artifact.toc",
  "artifact.view",
  "artifact.search_library",
  "artifact.list_events",
  "artifact.list_comments",
  "artifact.version_list",
  "artifact.version_diff",
] as const;

export class ArtifactsClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private nextId = 1;

  constructor(baseUrl: string, options: ArtifactsClientOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetch ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }

  async call<T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown> = {},
  ): Promise<T> {
    const id = this.nextId++;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.apiKey) {
      headers["X-API-Key"] = this.apiKey;
    }
    const res = await this.fetchImpl(`${this.baseUrl}/`, {
      method: "POST",
      headers,
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const payload = (await res.json()) as JsonRpcResponse<T>;
    if (payload.error) {
      throw new RpcError(payload.error.code, payload.error.message, payload.error.data);
    }
    return payload.result as T;
  }

  // ---- core ----

  ping(): Promise<Record<string, unknown>> {
    return this.call("ping");
  }

  createArtifact(params: CreateParams): Promise<{
    artifact: Artifact;
    version: ArtifactVersion;
    ref_text: string;
    deduplicated?: boolean;
  }> {
    return this.call("artifact.create", params as unknown as Record<string, unknown>);
  }

  getArtifact(artifactId: string, projectId?: string): Promise<{ artifact: Artifact }> {
    return this.call("artifact.get", { artifact_id: artifactId, project_id: projectId });
  }

  getContent(artifactId: string, version?: number | "latest"): Promise<Record<string, unknown>> {
    return this.call("artifact.get_content", { artifact_id: artifactId, version });
  }

  listArtifacts(sessionId: string, includeDeleted = false): Promise<{ artifacts: Artifact[] }> {
    return this.call("artifact.list", { session_id: sessionId, include_deleted: includeDeleted });
  }

  updateArtifact(params: UpdateParams): Promise<{ version: ArtifactVersion; ref_text: string }> {
    return this.call("artifact.update", params as unknown as Record<string, unknown>);
  }

  patchArtifact(artifactId: string, params: { operation?: string; anchor?: string; content?: string; expected_version?: number } = {}): Promise<Record<string, unknown>> {
    return this.call("artifact.patch", { artifact_id: artifactId, ...params });
  }

  loadArtifact(artifactId: string, previewOnly = true, section?: string): Promise<Record<string, unknown>> {
    return this.call("artifact.load", { artifact_id: artifactId, preview_only: previewOnly, section });
  }

  deleteArtifact(artifactId: string, softDelete = true): Promise<Record<string, unknown>> {
    return this.call("artifact.delete", { artifact_id: artifactId, soft_delete: softDelete });
  }

  listVersions(artifactId: string, page = 1, pageSize = 20): Promise<Record<string, unknown>> {
    return this.call("artifact.version_list", { artifact_id: artifactId, page, page_size: pageSize });
  }

  rollbackVersion(artifactId: string, targetVersion: number): Promise<Record<string, unknown>> {
    return this.call("artifact.version_rollback", { artifact_id: artifactId, target_version: targetVersion });
  }

  // ---- lifecycle ----

  renameArtifact(artifactId: string, newName: string): Promise<{ ok: boolean }> {
    return this.call("artifact.rename", { artifact_id: artifactId, new_name: newName });
  }

  starArtifact(artifactId: string, starred?: boolean): Promise<Record<string, unknown>> {
    return this.call("artifact.star", { artifact_id: artifactId, starred });
  }

  pinArtifact(artifactId: string, pinned?: boolean, chatId?: string): Promise<Record<string, unknown>> {
    return this.call("artifact.pin", { artifact_id: artifactId, pinned, chat_id: chatId });
  }

  duplicateArtifact(artifactId: string, newName?: string): Promise<Record<string, unknown>> {
    return this.call("artifact.duplicate", { artifact_id: artifactId, new_name: newName });
  }

  remixArtifact(artifactId: string, newName?: string): Promise<Record<string, unknown>> {
    return this.call("artifact.remix", { artifact_id: artifactId, new_name: newName });
  }

  // ---- recycle bin ----

  listRecycle(page = 1, pageSize = 20): Promise<Record<string, unknown>> {
    return this.call("artifact.list_recycle", { page, page_size: pageSize });
  }

  restoreArtifact(artifactId: string): Promise<Record<string, unknown>> {
    return this.call("artifact.restore", { artifact_id: artifactId });
  }

  purgeExpired(): Promise<Record<string, unknown>> {
    return this.call("artifact.purge_expired", {});
  }

  // ---- share ----

  createShare(params: CreateShareParams): Promise<{ share: Record<string, unknown>; share_url: string }> {
    return this.call("artifact.create_share", params as unknown as Record<string, unknown>);
  }

  getShared(shareId: string): Promise<Record<string, unknown>> {
    return this.call("artifact.get_shared", { share_id: shareId });
  }

  revokeShare(shareId: string): Promise<Record<string, unknown>> {
    return this.call("artifact.revoke_share", { share_id: shareId });
  }

  // ---- snapshots ----

  createSnapshot(artifactId: string, label?: string, author?: string): Promise<Record<string, unknown>> {
    return this.call("artifact.create_snapshot", { artifact_id: artifactId, label, author });
  }

  listSnapshots(artifactId: string, page = 1, pageSize = 20): Promise<Record<string, unknown>> {
    return this.call("artifact.list_snapshots", { artifact_id: artifactId, page, page_size: pageSize });
  }
}
