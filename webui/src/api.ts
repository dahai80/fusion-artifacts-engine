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
    content_hash: string | null;
    created_at: number;
    updated_at: number;
    is_starred: boolean;
    is_pinned: boolean;
    share_id: string | null;
    source_module: string | null;
    workflow_run_id: string | null;
    parent_artifact_id: string | null;
    folder_id: string | null;
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

// #116: folder organization
export interface ArtifactFolder {
    folder_id: string;
    name: string;
    parent_id: string | null;
    project_id: string | null;
    created_at: number | null;
}

export async function listFolders(): Promise<ArtifactFolder[]> {
    const r = await rpc("artifact.list_folders", {});
    return r.folders;
}

export async function createFolder(name: string, parentId?: string): Promise<ArtifactFolder> {
    const r = await rpc("artifact.create_folder", { name, parent_id: parentId || undefined });
    return r.folder;
}

export async function renameFolder(folderId: string, newName: string): Promise<void> {
    await rpc("artifact.rename_folder", { folder_id: folderId, new_name: newName });
}

export async function deleteFolder(folderId: string): Promise<void> {
    await rpc("artifact.delete_folder", { folder_id: folderId });
}

export async function moveToFolder(artifactId: string, folderId: string | null): Promise<void> {
    await rpc("artifact.move_to_folder", { artifact_id: artifactId, folder_id: folderId });
}

// #116: tags & pinning
export interface ArtifactTag {
    tag_id: string;
    name: string;
    color: string | null;
    scope: string | null;
}

export async function listTags(): Promise<ArtifactTag[]> {
    const r = await rpc("artifact.list_tags", {});
    return r.tags;
}

export async function listArtifactTags(artifactId: string): Promise<ArtifactTag[]> {
    const r = await rpc("artifact.list_artifact_tags", { artifact_id: artifactId });
    return r.tags;
}

export async function addTag(artifactId: string, tagName: string): Promise<ArtifactTag> {
    const r = await rpc("artifact.add_tag", { artifact_id: artifactId, tag_name: tagName });
    return r.tag;
}

export async function removeTag(artifactId: string, tagName: string): Promise<void> {
    await rpc("artifact.remove_tag", { artifact_id: artifactId, tag_name: tagName });
}

export async function pinArtifact(artifactId: string, pinned: boolean): Promise<void> {
    await rpc("artifact.pin", { artifact_id: artifactId, pinned });
}

export async function starArtifact(artifactId: string, starred: boolean): Promise<void> {
    await rpc("artifact.star", { artifact_id: artifactId, starred });
}

// #116: lifecycle ops
export async function renameArtifact(artifactId: string, newName: string): Promise<void> {
    await rpc("artifact.rename", { artifact_id: artifactId, new_name: newName });
}

export async function duplicateArtifact(artifactId: string): Promise<Artifact> {
    const r = await rpc("artifact.duplicate", { artifact_id: artifactId });
    return r.artifact;
}

// #116: snapshots
export interface ArtifactSnapshot {
    version_num: number;
    snapshot_label: string | null;
    author: string | null;
    created_at: number;
}

export async function createSnapshot(artifactId: string, label: string): Promise<ArtifactSnapshot> {
    const r = await rpc("artifact.create_snapshot", { artifact_id: artifactId, label });
    return r.version;
}

export async function listSnapshots(artifactId: string): Promise<ArtifactSnapshot[]> {
    const r = await rpc("artifact.list_snapshots", { artifact_id: artifactId, include_content: false });
    return r.snapshots;
}

// #116: code exchange
export async function exportCode(artifactId: string, language = ""): Promise<{ code: string; language: string; ext: string }> {
    return rpc("artifact.export_code", { artifact_id: artifactId, language });
}

export async function importCode(code: string, language = "", name = ""): Promise<Artifact> {
    const r = await rpc("artifact.import_code", { session_id: "webui-import", code, language, name });
    return r.artifact;
}

// #116: maintenance / AI hygiene
export async function autoCompact(artifactId: string, tokenBudget: number): Promise<Record<string, any>> {
    return rpc("artifact.auto_compact", { artifact_id: artifactId, token_budget: tokenBudget });
}

export async function purgeExpired(): Promise<number> {
    const r = await rpc("artifact.purge_expired", {});
    return r.purged;
}

export async function checkSafety(
    messages: { role: string; content: string }[],
    outputBudget?: number
): Promise<Record<string, any>> {
    const params: Record<string, any> = { messages };
    if (outputBudget) params.output_budget = outputBudget;
    return rpc("artifact.check_safety", params);
}

export async function moveToProjectKb(artifactId: string, projectId: string): Promise<void> {
    await rpc("artifact.move_to_project_kb", { artifact_id: artifactId, project_id: projectId });
}

export async function listBySource(sourceModule: string): Promise<Artifact[]> {
    const r = await rpc("artifact.list_by_source", { source_module: sourceModule });
    return r.artifacts;
}

export async function listRecycle(): Promise<{ artifacts: Artifact[]; total: number }> {
    return rpc("artifact.list_recycle", { page: 1, page_size: 50 });
}

export async function createArtifact(
    name: string,
    artifactType: string,
    content: string,
    sessionId = "webui-created"
): Promise<Artifact> {
    const r = await rpc("artifact.create", {
        session_id: sessionId,
        name,
        type: artifactType,
        content,
        metadata: { source: "webui" },
    });
    return r.artifact;
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

export async function updateArtifact(
    id: string,
    content: string,
    expectedContentHash: string | null,
    changeLog = "",
    source: "manual" | "ai_generation" = "manual"
): Promise<void> {
    const params: Record<string, any> = { artifact_id: id, content, source, change_log: changeLog };
    if (expectedContentHash) params.expected_content_hash = expectedContentHash;
    await rpc("artifact.update", params);
}

export async function aiGenerate(
    prompt: string,
    system?: string,
    model?: string
): Promise<{ text: string; model: string }> {
    const params: Record<string, any> = { prompt };
    if (system) params.system = system;
    if (model) params.model = model;
    return rpc("ai.generate", params);
}

export async function aiListModels(): Promise<{ models: string[]; default: string }> {
    return rpc("ai.list_models", {});
}

export interface PublicShare {
    status: "ok" | "gone" | "not_found";
    reason?: string;
    share?: { share_id: string; permission: "view" | "remix"; expires_at: number | null };
    artifact?: { id: string; name: string; type: string; kind: string | null; summary: string; updated_at: number };
    rendered_html?: string;
}

export async function getPublicShare(shareId: string): Promise<PublicShare> {
    const resp = await fetch(`${BASE}api/v1/share/${encodeURIComponent(shareId)}`);
    if (resp.status === 410) {
        const body = await resp.json().catch(() => ({}));
        return { status: "gone", reason: body.reason };
    }
    if (!resp.ok) return { status: "not_found" };
    return resp.json();
}

let shareBasePromise: Promise<string> | null = null;

export async function getShareBase(): Promise<string> {
    if (!shareBasePromise) {
        shareBasePromise = fetch(`${BASE}api/v1/share-base`)
            .then(async (resp) => {
                if (!resp.ok) throw new Error(`share-base ${resp.status}`);
                const body = await resp.json();
                return String(body.base_url || "").replace(/\/$/, "");
            })
            .catch((e) => {
                shareBasePromise = null;
                throw e;
            });
    }
    return shareBasePromise;
}
