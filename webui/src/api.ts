const BASE = window.location.pathname.endsWith("/")
    ? window.location.pathname
    : window.location.pathname + "/";

let reqId = 0;

export interface RpcResponse {
    jsonrpc: string;
    id: number;
    result?: any;
    error?: { code: number; message: string };
}

export async function rpc(method: string, params: Record<string, any> = {}): Promise<any> {
    const body = JSON.stringify({ jsonrpc: "2.0", method, params, id: ++reqId });
    const resp = await fetch(BASE, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
    });
    const data: RpcResponse = await resp.json();
    if (data.error) {
        throw new Error(`${data.error.code}: ${data.error.message}`);
    }
    return data.result;
}

export interface Artifact {
    id: string;
    session_id: string;
    name: string;
    type: string;
    kind: string | null;
    project_id: string | null;
    metadata: Record<string, any> | null;
    current_version: number;
    summary: string;
    created_at: number;
    updated_at: number;
    is_starred: boolean;
    is_pinned: boolean;
    share_id: string | null;
    source_module: string | null;
    workflow_run_id: string | null;
    parent_artifact_id: string | null;
}

export interface ArtifactVersion {
    artifact_id: string;
    version_num: number;
    size_bytes: number;
    token_count: number;
    change_log: string;
    source: string;
    created_at: number;
}

export async function listArtifacts(filters: Record<string, any> = {}): Promise<{ artifacts: Artifact[]; total: number }> {
    return rpc("artifact.list_all", { filters, page: 1, page_size: 100, sort: "updated_at" });
}

export async function listRecycle(): Promise<{ artifacts: Artifact[]; total: number }> {
    return rpc("artifact.list_recycle", { page: 1, page_size: 50 });
}

export async function getArtifact(id: string): Promise<Artifact> {
    const r = await rpc("artifact.get", { artifact_id: id });
    return r.artifact;
}

export async function getContent(id: string, version?: number): Promise<string> {
    const params: Record<string, any> = { artifact_id: id };
    if (version) params.version = version;
    const r = await rpc("artifact.get_content", params);
    return r.content;
}

export async function listVersions(id: string): Promise<ArtifactVersion[]> {
    const r = await rpc("artifact.version_list", { artifact_id: id, include_content: false, page: 1, page_size: 200 });
    return r.versions;
}

export async function versionDiff(id: string, fromVersion: number, toVersion: number): Promise<string> {
    const r = await rpc("artifact.version_diff", { artifact_id: id, from_version: fromVersion, to_version: toVersion });
    return r.diff;
}

export async function rollback(id: string, targetVersion: number): Promise<void> {
    await rpc("artifact.version_rollback", { artifact_id: id, target_version: targetVersion });
}

export async function listEvents(id: string): Promise<any[]> {
    const r = await rpc("artifact.list_events", { artifact_id: id, page: 1, page_size: 100 });
    return r.events ?? r;
}

export async function createShare(id: string, permission: "view" | "remix" = "view"): Promise<{ share_id: string; permission: string }> {
    const r = await rpc("artifact.create_share", { artifact_id: id, permission });
    return r.share;
}

export async function revokeShare(shareId: string): Promise<void> {
    await rpc("artifact.revoke_share", { share_id: shareId });
}

export async function remixArtifact(id: string): Promise<Artifact> {
    const r = await rpc("artifact.remix", { artifact_id: id });
    return r.artifact;
}

export async function restoreArtifact(id: string): Promise<void> {
    await rpc("artifact.restore", { artifact_id: id });
}
