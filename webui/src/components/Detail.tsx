import { useEffect, useMemo, useState } from "react";
import { marked } from "marked";
import {
    ArrowLeft,
    Camera,
    Check,
    Copy,
    Download,
    FolderInput,
    GitCompare,
    History,
    Link2,
    MessageSquare,
    MessagesSquare,
    Pencil,
    RotateCcw,
    Save,
    ShieldCheck,
    Sparkles,
    Tags,
    MessageCircle,
    Wrench,
    X,
    GitFork,
} from "lucide-react";
import {
    addComment,
    addTag,
    aiGenerate,
    aiListModels,
    autoCompact,
    checkSafety,
    createShare,
    createSnapshot,
    listComments,
    type ReviewComment,
    duplicateArtifact,
    exportCode,
    getArtifact,
    getContent,
    getShareBase,
    listArtifactTags,
    listEvents,
    listFolders,
    listSnapshots,
    listVersions,
    moveToFolder,
    moveToProjectKb,
    removeTag,
    remixArtifact,
    renameArtifact,
    revokeShare,
    rollback,
    updateArtifact,
    versionDiff,
    type Artifact,
    type ArtifactFolder,
    type ArtifactSnapshot,
    type ArtifactTag,
    type ArtifactVersion,
} from "../api";
import { t, type Lang } from "../i18n";
import { relTime, sourceLabel, stripFence } from "./Library";

type PreviewMode = "preview" | "code" | "diff";

function fmtBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export default function Detail({
    id,
    lang,
    onBack,
    initialVersion,
}: {
    id: string;
    lang: Lang;
    onBack: () => void;
    initialVersion?: number;
}) {
    const d = t(lang);
    const [artifact, setArtifact] = useState<Artifact | null>(null);
    const [versions, setVersions] = useState<ArtifactVersion[]>([]);
    const [content, setContent] = useState("");
    const [sessions, setSessions] = useState<string[]>([]);
    const [mode, setMode] = useState<PreviewMode>("preview");
    const [diffText, setDiffText] = useState("");
    const [diffFrom, setDiffFrom] = useState<number | null>(null);
    const [error, setError] = useState("");
    const [copied, setCopied] = useState(false);
    const [busy, setBusy] = useState(false);
    const [sharePerm, setSharePerm] = useState<"view" | "remix">("view");
    const [shareBase, setShareBase] = useState("");
    const [parentName, setParentName] = useState("");
    // S6: native confirm is banned — blocking confirmation goes through our own Modal (bridge until a ConfirmDialog component lands)
    const [confirm, setConfirm] = useState<{ msg: string; action: () => void } | null>(null);
    // #96: content editor + optimistic locking (expected_content_hash; conflict -32002 surfaces an explicit error)
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState("");
    const [saving, setSaving] = useState(false);
    // #97: AI iteration panel (ai.generate → artifact.update source=ai_generation)
    const [aiPrompt, setAiPrompt] = useState("");
    const [aiBusy, setAiBusy] = useState(false);
    const [aiModels, setAiModels] = useState<string[]>([]);
    const [aiModel, setAiModel] = useState("");
    // #116: snapshots timeline
    const [snapshots, setSnapshots] = useState<ArtifactSnapshot[]>([]);
    const [snapshotLabel, setSnapshotLabel] = useState("");
    const [snapshotBusy, setSnapshotBusy] = useState(false);
    // #116: organization (rename / duplicate / folder / tags / project KB)
    const [folders, setFolders] = useState<ArtifactFolder[]>([]);
    const [artifactTags, setArtifactTags] = useState<ArtifactTag[]>([]);
    const [tagInput, setTagInput] = useState("");
    const [renameOpen, setRenameOpen] = useState(false);
    const [renameValue, setRenameValue] = useState("");
    const [kbProjectId, setKbProjectId] = useState("");
    // #116: maintenance (auto compact / safety check / export code)
    const [compactBudget, setCompactBudget] = useState("");
    const [compactNote, setCompactNote] = useState("");
    const [safetyNote, setSafetyNote] = useState("");
    const [codeExported, setCodeExported] = useState("");
    const [maintBusy, setMaintBusy] = useState(false);
    // #119: version-pinned deep link + review comments
    const [viewVersion, setViewVersion] = useState<number | null>(initialVersion || null);
    const [comments, setComments] = useState<ReviewComment[]>([]);
    const [commentInput, setCommentInput] = useState("");
    const [commentBusy, setCommentBusy] = useState(false);

    useEffect(() => {
        getShareBase()
            .then(setShareBase)
            .catch(() => setShareBase(""));
    }, []);

    const load = async () => {
        try {
            const [a, vs] = await Promise.all([getArtifact(id), listVersions(id)]);
            setArtifact(a);
            setVersions(vs);
            // #119: deep link ?v=N pins the content to that version (current version shows live content)
            const pinned = viewVersion && viewVersion !== a.current_version ? viewVersion : null;
            const c = pinned ? await getContent(id, pinned) : await getContent(id);
            setContent(c);
            if (!pinned && viewVersion && viewVersion === a.current_version) setViewVersion(null);
            setError("");
            try {
                const ev = await listEvents(id);
                const ids = new Set<string>([a.session_id]);
                for (const e of ev) {
                    if (e.session_id) ids.add(e.session_id);
                }
                setSessions([...ids].slice(0, 8));
            } catch {
                setSessions([a.session_id]);
            }
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    useEffect(() => {
        load();
    }, [id]);

    // #116: snapshots + folders + tags load alongside the artifact
    useEffect(() => {
        listSnapshots(id)
            .then((s) => setSnapshots([...s].sort((x, y) => y.version_num - x.version_num)))
            .catch(() => setSnapshots([]));
        listFolders()
            .then(setFolders)
            .catch(() => setFolders([]));
        listArtifactTags(id)
            .then(setArtifactTags)
            .catch(() => setArtifactTags([]));
    }, [id]);

    const doSnapshot = async () => {
        setSnapshotBusy(true);
        try {
            await createSnapshot(id, snapshotLabel.trim());
            setSnapshotLabel("");
            const s = await listSnapshots(id);
            setSnapshots([...s].sort((x, y) => y.version_num - x.version_num));
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setSnapshotBusy(false);
        }
    };

    const doRename = async () => {
        const name = renameValue.trim();
        if (!name) return;
        try {
            await renameArtifact(id, name);
            setRenameOpen(false);
            await load();
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    const doDuplicate = () => {
        setConfirm({
            msg: d.confirmDuplicate,
            action: async () => {
                try {
                    const dup = await duplicateArtifact(id);
                    window.location.hash = `#/a/${dup.id}`;
                } catch (e) {
                    setError(`${d.loadError}: ${(e as Error).message}`);
                }
            },
        });
    };

    const doMoveFolder = async (folderId: string) => {
        try {
            await moveToFolder(id, folderId || null);
            await load();
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    const doAddTag = async () => {
        const name = tagInput.trim();
        if (!name) return;
        try {
            await addTag(id, name);
            setTagInput("");
            setArtifactTags(await listArtifactTags(id));
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    const doRemoveTag = async (name: string) => {
        try {
            await removeTag(id, name);
            setArtifactTags(await listArtifactTags(id));
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    const doMoveKb = async () => {
        const pid = kbProjectId.trim();
        if (!pid) return;
        try {
            await moveToProjectKb(id, pid);
            setError("");
            setSafetyNote("");
            setCompactNote(`${d.moveKb}: ${pid}`);
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    const doCompact = async () => {
        const budget = parseInt(compactBudget, 10);
        if (!budget || budget <= 0) return;
        setMaintBusy(true);
        try {
            const r = await autoCompact(id, budget);
            setCompactNote(
                r.compacted
                    ? `${r.original_tokens} → ${r.compacted_tokens} tokens (-${Math.round(r.savings_pct)}%)`
                    : d.compactNoNeed
            );
            await load();
        } catch (e) {
            setCompactNote(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setMaintBusy(false);
        }
    };

    const doSafety = async () => {
        setMaintBusy(true);
        try {
            const r = await checkSafety([{ role: "user", content }]);
            setSafetyNote(
                r.safe
                    ? `${d.safetyOk} (${r.current_tokens}/${r.effective_budget})`
                    : `${r.current_tokens}/${r.effective_budget}`
            );
        } catch (e) {
            setSafetyNote(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setMaintBusy(false);
        }
    };

    const doExportCode = async () => {
        try {
            const r = await exportCode(id);
            setCodeExported(r.code);
            await navigator.clipboard.writeText(r.code);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    // #119: review comments — list on load, add attaches to the pinned (or current) version
    useEffect(() => {
        listComments(id)
            .then(setComments)
            .catch(() => setComments([]));
    }, [id]);

    const doAddComment = async () => {
        const text = commentInput.trim();
        if (!text || commentBusy) return;
        const ver = viewVersion || artifact?.current_version;
        if (!ver) return;
        setCommentBusy(true);
        try {
            await addComment(id, ver, text, "webui");
            setCommentInput("");
            setComments(await listComments(id));
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setCommentBusy(false);
        }
    };

    useEffect(() => {
        // #77: remix lineage — fetch parent name for the header badge
        if (!artifact?.parent_artifact_id) {
            setParentName("");
            return;
        }
        getArtifact(artifact.parent_artifact_id)
            .then((p) => setParentName(p.name))
            .catch(() => setParentName(artifact.parent_artifact_id || ""));
    }, [artifact?.parent_artifact_id]);

    // #100-1: mermaid is rendered to SVG in the module context then injected as a blob — the blob iframe inherits
    // the page CSP (script-src 'self'), which blocks both CDN external scripts and inline scripts, so a render-shell
    // approach is not viable; bundle mermaid locally with zero scripts inside the iframe, satisfying offline/local-first at once.
    const [mermaidSvg, setMermaidSvg] = useState("");
    useEffect(() => {
        let alive = true;
        if (!artifact || artifact.type !== "mermaid" || !content) {
            setMermaidSvg("");
            return;
        }
        import("mermaid")
            .then(async (m) => {
                try {
                    m.default.initialize({ startOnLoad: false });
                    const { svg } = await m.default.render(
                        "mmd-" + artifact.id,
                        content
                    );
                    if (alive) setMermaidSvg(svg);
                } catch (e) {
                    console.error("mermaid render failed", e);
                    if (alive) setMermaidSvg("");
                }
            })
            .catch((e) => {
                console.error("mermaid load failed", e);
                if (alive) setMermaidSvg("");
            });
        return () => {
            alive = false;
        };
    }, [artifact, content]);

    const blobUrl = useMemo(() => {
        if (!artifact || !content) return "";
        const ty = artifact.type;
        if (ty !== "html" && ty !== "react" && ty !== "svg" && ty !== "mermaid")
            return "";
        const doc =
            ty === "mermaid"
                ? `<!DOCTYPE html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:16px;background:#fff;display:flex;justify-content:center}svg{max-width:100%;height:auto}</style></head><body>${mermaidSvg}</body></html>`
                : content;
        if (ty === "mermaid" && !mermaidSvg) return "";
        return URL.createObjectURL(new Blob([doc], { type: "text/html" }));
    }, [artifact, content, mermaidSvg]);

    useEffect(() => {
        return () => {
            if (blobUrl) URL.revokeObjectURL(blobUrl);
        };
    }, [blobUrl]);

    const renderedMd = useMemo(() => {
        if (!artifact || artifact.type !== "markdown") return "";
        return marked.parse(content, { async: false }) as string;
    }, [artifact, content]);

    const doDiff = async (fromVersion: number) => {
        if (!artifact) return;
        const target = artifact.current_version;
        if (fromVersion === target) return;
        const r = await versionDiff(id, fromVersion, target);
        setDiffText(r || d.diffEmpty);
        setDiffFrom(fromVersion);
        setMode("diff");
    };

    const doRestore = (targetVersion: number) => {
        setConfirm({
            msg: d.confirmRestore,
            action: async () => {
                setBusy(true);
                try {
                    await rollback(id, targetVersion);
                    setMode("preview");
                    setDiffFrom(null);
                    await load();
                } catch (e) {
                    setError(`${d.loadError}: ${(e as Error).message}`);
                } finally {
                    setBusy(false);
                }
            },
        });
    };

    const shareUrl = artifact?.share_id
        ? shareBase
            ? `${shareBase}/s/${artifact.share_id}`
            : `${window.location.origin}/s/${artifact.share_id}`
        : "";

    const doShare = async () => {
        setBusy(true);
        try {
            await createShare(id, sharePerm);
            await load();
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setBusy(false);
        }
    };

    const doRemix = () => {
        setConfirm({
            msg: d.confirmRemix,
            action: async () => {
                setBusy(true);
                try {
                    const child = await remixArtifact(id);
                    window.location.hash = `#/a/${child.id}`;
                } catch (e) {
                    setError(`${d.loadError}: ${(e as Error).message}`);
                    setBusy(false);
                }
            },
        });
    };

    const doRevoke = async () => {
        if (!artifact?.share_id) return;
        setBusy(true);
        try {
            await revokeShare(artifact.share_id);
            await load();
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setBusy(false);
        }
    };

    const copyLink = async () => {
        await navigator.clipboard.writeText(shareUrl);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
    };

    const startEdit = () => {
        setDraft(content);
        setEditing(true);
        setMode("code");
    };

    const doSaveEdit = async () => {
        if (!artifact) return;
        setSaving(true);
        try {
            await updateArtifact(id, draft, artifact.content_hash, d.manualEdit, "manual");
            setEditing(false);
            await load();
        } catch (e) {
            const msg = (e as Error).message;
            setError(msg.startsWith("-32002") ? d.conflictMsg : `${d.loadError}: ${msg}`);
        } finally {
            setSaving(false);
        }
    };

    const openAiPanel = () => {
        if (aiModels.length === 0) {
            aiListModels()
                .then((r) => setAiModels(r.models.length > 0 ? r.models : []))
                .catch(() => {});
        }
    };

    const doAiIterate = async () => {
        if (!artifact || !aiPrompt.trim()) return;
        setAiBusy(true);
        try {
            const sys = `${d.aiCard}: 基于当前产物内容按用户要求修改，输出完整新内容（不是 diff），不要解释，不要 markdown 代码围栏。\n\n<当前内容>\n${content}\n</当前内容>`;
            const r = await aiGenerate(aiPrompt, sys, aiModel || undefined);
            const text = stripFence(r.text);
            await updateArtifact(
                id,
                text,
                artifact.content_hash,
                `AI: ${aiPrompt.trim().slice(0, 40)}`,
                "ai_generation"
            );
            setAiPrompt("");
            await load();
        } catch (e) {
            const msg = (e as Error).message;
            setError(msg.startsWith("-32002") ? d.conflictMsg : `${d.loadError}: ${msg}`);
        } finally {
            setAiBusy(false);
        }
    };

    if (error && !artifact) {
        return (
            <div className="mx-auto w-full max-w-5xl px-6 py-6">
                <p className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
                    {error}
                </p>
            </div>
        );
    }

    if (!artifact) {
        return (
            <div className="flex h-full items-center justify-center text-sm text-zinc-400">
                {d.loadError}…
            </div>
        );
    }

    const isHtml =
        artifact.type === "html" ||
        artifact.type === "react" ||
        artifact.type === "svg" ||
        artifact.type === "mermaid";
    const isMd = artifact.type === "markdown";
    const current = artifact.current_version;

    return (
        <div className="mx-auto w-full max-w-5xl px-6 py-6">
            <button
                onClick={onBack}
                className="mb-4 flex items-center gap-2 text-sm text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"
            >
                <ArrowLeft size={16} />
                {d.back}
            </button>

            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-xl font-semibold">{artifact.name}</h1>
                    <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                        {d.source} {sourceLabel(artifact, lang)} · {d.fromSession} {artifact.session_id} ·{" "}
                        {relTime(artifact.updated_at, lang)}
                    </p>
                    {artifact.parent_artifact_id && (
                        <button
                            onClick={() => (window.location.hash = `#/a/${artifact.parent_artifact_id}`)}
                            className="mt-1 flex items-center gap-1 text-xs text-indigo-500 transition hover:text-indigo-600"
                        >
                            <GitFork size={16} />
                            {d.remixOf} {parentName || artifact.parent_artifact_id}
                        </button>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    {!editing && (
                        <button
                            disabled={busy}
                            onClick={() => {
                                setRenameValue(artifact.name);
                                setRenameOpen(!renameOpen);
                            }}
                            className="flex items-center gap-1 rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                            title={d.rename}
                        >
                            <Pencil size={16} />
                            {d.rename}
                        </button>
                    )}
                    {!editing && (
                        <button
                            disabled={busy}
                            onClick={doDuplicate}
                            className="flex items-center gap-1 rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                            title={d.duplicate}
                        >
                            <Copy size={16} />
                            {d.duplicate}
                        </button>
                    )}
                    {!editing && (
                        <button
                            disabled={busy}
                            onClick={doExportCode}
                            className="flex items-center gap-1 rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                            title={d.exportCode}
                        >
                            {copied ? <Check size={16} /> : <Download size={16} />}
                            {d.exportCode}
                        </button>
                    )}
                    {!editing && (
                        <button
                            disabled={busy}
                            onClick={startEdit}
                            className="flex items-center gap-1 rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                            title={d.edit}
                        >
                            <Pencil size={16} />
                            {d.edit}
                        </button>
                    )}
                    <button
                        disabled={busy}
                        onClick={doRemix}
                        className="flex items-center gap-1 rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                        title={d.remix}
                    >
                        <GitFork size={16} />
                        {d.remix}
                    </button>
                    <button
                        onClick={() =>
                            window.open(
                                `/shell/fusion-cowork/?artifact_ref=${encodeURIComponent(id)}`,
                                "_blank"
                            )
                        }
                        className="flex items-center gap-1 rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                        title={d.continueChat}
                    >
                        <MessagesSquare size={16} />
                        {d.continueChat}
                    </button>
                    {isHtml && (
                        <ModeBtn active={mode === "preview"} onClick={() => setMode("preview")}>
                            {d.runPreview}
                        </ModeBtn>
                    )}
                    {isMd && (
                        <ModeBtn active={mode === "preview"} onClick={() => setMode("preview")}>
                            {d.docRender}
                        </ModeBtn>
                    )}
                    <ModeBtn active={mode === "code"} onClick={() => setMode("code")}>
                        {d.codeView}
                    </ModeBtn>
                    {mode === "diff" && (
                        <ModeBtn active onClick={() => { setMode("preview"); setDiffFrom(null); }}>
                            <span className="flex items-center gap-1">
                                <X size={16} /> {d.close}
                            </span>
                        </ModeBtn>
                    )}
                </div>
            </div>

            {error && (
                <p className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
                    {error}
                </p>
            )}

            {/* #119: version-pinned deep-link banner */}
            {viewVersion && artifact && viewVersion !== artifact.current_version && (
                <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
                    <span>
                        {d.viewingVersion} v{viewVersion} ({d.current} v{artifact.current_version})
                    </span>
                    <button
                        onClick={() => {
                            setViewVersion(null);
                            getContent(id).then(setContent);
                        }}
                        className="rounded-md border border-amber-400/60 px-3 py-1 text-xs font-medium transition hover:bg-amber-100 dark:hover:bg-amber-500/20"
                    >
                        {d.backToCurrent}
                    </button>
                </div>
            )}

            {renameOpen && (
                <div className="mb-4 flex items-center gap-2 rounded-lg border border-zinc-200/80 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
                    <input
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter") doRename();
                            if (e.key === "Escape") setRenameOpen(false);
                        }}
                        autoFocus
                        placeholder={d.newNamePh}
                        className="min-w-0 flex-1 rounded-md border border-zinc-200/80 bg-white px-3 py-2 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                    />
                    <button
                        onClick={doRename}
                        disabled={!renameValue.trim()}
                        className="flex items-center gap-1 rounded-md bg-indigo-500 px-3 py-2 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                    >
                        <Save size={16} />
                        {d.save}
                    </button>
                    <button
                        onClick={() => setRenameOpen(false)}
                        className="rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                    >
                        {d.close}
                    </button>
                </div>
            )}

            <div className="flex flex-col gap-4 lg:flex-row">
                <div className="min-w-0 flex-1">
                    <div className="overflow-hidden rounded-lg border border-zinc-200/80 bg-white dark:border-zinc-800 dark:bg-zinc-900">
                        {mode === "preview" && isHtml && blobUrl && (
                            <iframe
                                src={blobUrl}
                                title="preview"
                                sandbox="allow-scripts allow-forms allow-popups"
                                className="h-[480px] w-full border-0 bg-white"
                            />
                        )}
                        {mode === "preview" && isMd && (
                            <div
                                className="prose prose-zinc dark:prose-invert max-w-none p-6 text-sm [&_a]:text-indigo-500"
                                dangerouslySetInnerHTML={{ __html: renderedMd }}
                            />
                        )}
                        {(mode === "code" || (mode === "preview" && !isHtml && !isMd)) && !editing && (
                            <pre className="max-h-[480px] overflow-auto p-4 text-xs leading-relaxed text-zinc-700 dark:text-zinc-300">
                                <code>{content}</code>
                            </pre>
                        )}
                        {editing && (
                            <>
                                <textarea
                                    value={draft}
                                    onChange={(e) => setDraft(e.target.value)}
                                    spellCheck={false}
                                    className="h-[480px] w-full resize-none border-0 bg-transparent p-4 font-mono text-xs leading-relaxed text-zinc-800 outline-none dark:text-zinc-200"
                                />
                                <div className="flex items-center justify-end gap-2 border-t border-zinc-100 px-4 py-3 dark:border-zinc-800">
                                    <button
                                        onClick={() => setEditing(false)}
                                        className="rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                                    >
                                        {d.close}
                                    </button>
                                    <button
                                        disabled={saving || draft === content}
                                        onClick={doSaveEdit}
                                        className="flex items-center gap-1 rounded-md bg-indigo-500 px-3 py-2 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                                    >
                                        <Save size={16} />
                                        {d.save}
                                    </button>
                                </div>
                            </>
                        )}
                        {mode === "diff" && (
                            <pre className="max-h-[480px] overflow-auto p-4 text-xs leading-relaxed">
                                {diffText.split("\n").map((line, i) => (
                                    <div
                                        key={i}
                                        className={
                                            line.startsWith("+") && !line.startsWith("+++")
                                                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                                                : line.startsWith("-") && !line.startsWith("---")
                                                  ? "bg-red-500/10 text-red-700 dark:text-red-400"
                                                  : line.startsWith("@@")
                                                    ? "text-indigo-500"
                                                    : "text-zinc-600 dark:text-zinc-400"
                                        }
                                    >
                                        {line || " "}
                                    </div>
                                ))}
                            </pre>
                        )}
                    </div>
                    {diffFrom !== null && mode === "diff" && (
                        <p className="mt-2 text-xs text-zinc-400">
                            <GitCompare size={16} className="mr-1 inline" />
                            v{diffFrom} → v{current}
                        </p>
                    )}

                    <section className="mt-6">
                        <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-zinc-500 dark:text-zinc-400">
                            <MessageSquare size={16} />
                            {d.referencing} ({sessions.length})
                        </h2>
                        {sessions.length === 0 ? (
                            <p className="text-xs text-zinc-400">{d.noSessions}</p>
                        ) : (
                            <div className="flex flex-wrap gap-2">
                                {sessions.map((s) => (
                                    <span
                                        key={s}
                                        className="rounded-lg bg-zinc-100 px-2 py-1 font-mono text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
                                    >
                                        {s}
                                    </span>
                                ))}
                            </div>
                        )}
                    </section>
                </div>

                <aside className="w-full shrink-0 space-y-4 lg:w-72">
                    {/* #116: organization — folder / tags / project KB */}
                    <div className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <FolderInput size={16} className="text-zinc-400" />
                            {d.moveFolder}
                        </h2>
                        <select
                            value={artifact.folder_id || ""}
                            onChange={(e) => doMoveFolder(e.target.value)}
                            className="w-full rounded-md border border-zinc-200/80 bg-white px-2 py-1.5 text-xs text-zinc-600 outline-none dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300"
                        >
                            <option value="">{d.noFolder}</option>
                            {folders.map((f) => (
                                <option key={f.folder_id} value={f.folder_id}>
                                    {f.name}
                                </option>
                            ))}
                        </select>
                        <h2 className="mb-2 mt-4 flex items-center gap-2 text-sm font-semibold">
                            <Tags size={16} className="text-zinc-400" />
                            {d.tags}
                        </h2>
                        {artifactTags.length > 0 && (
                            <div className="mb-2 flex flex-wrap gap-1">
                                {artifactTags.map((tg) => (
                                    <span
                                        key={tg.tag_id}
                                        className="flex items-center gap-1 rounded-full bg-violet-500/10 px-2 py-0.5 text-xs font-medium text-violet-600 dark:text-violet-400"
                                    >
                                        {tg.name}
                                        <button
                                            onClick={() => doRemoveTag(tg.name)}
                                            className="text-violet-400 transition hover:text-violet-600"
                                        >
                                            <X size={12} />
                                        </button>
                                    </span>
                                ))}
                            </div>
                        )}
                        <div className="flex items-center gap-1">
                            <input
                                value={tagInput}
                                onChange={(e) => setTagInput(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === "Enter") doAddTag();
                                }}
                                placeholder={d.tagPh}
                                className="min-w-0 flex-1 rounded-md border border-zinc-200/80 bg-white px-2 py-1.5 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                            />
                            <button
                                onClick={doAddTag}
                                disabled={!tagInput.trim()}
                                className="shrink-0 rounded-md bg-indigo-500/10 px-2 py-1.5 text-xs font-medium text-indigo-600 transition hover:bg-indigo-500/20 disabled:opacity-40 dark:text-indigo-400"
                            >
                                {d.addTag}
                            </button>
                        </div>
                        <h2 className="mb-2 mt-4 flex items-center gap-2 text-sm font-semibold">
                            <GitFork size={16} className="text-zinc-400" />
                            {d.moveToKb}
                        </h2>
                        <div className="flex items-center gap-1">
                            <input
                                value={kbProjectId}
                                onChange={(e) => setKbProjectId(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === "Enter") doMoveKb();
                                }}
                                placeholder={d.projectIdPh}
                                className="min-w-0 flex-1 rounded-md border border-zinc-200/80 bg-white px-2 py-1.5 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                            />
                            <button
                                onClick={doMoveKb}
                                disabled={!kbProjectId.trim()}
                                className="shrink-0 rounded-md bg-indigo-500/10 px-2 py-1.5 text-xs font-medium text-indigo-600 transition hover:bg-indigo-500/20 disabled:opacity-40 dark:text-indigo-400"
                            >
                                {d.moveKb}
                            </button>
                        </div>
                    </div>

                    {/* #116: snapshots timeline */}
                    <div className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <Camera size={16} className="text-zinc-400" />
                            {d.snapshots} ({snapshots.length})
                        </h2>
                        <div className="flex items-center gap-1">
                            <input
                                value={snapshotLabel}
                                onChange={(e) => setSnapshotLabel(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === "Enter") doSnapshot();
                                }}
                                placeholder={d.snapshotLabelPh}
                                className="min-w-0 flex-1 rounded-md border border-zinc-200/80 bg-white px-2 py-1.5 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                            />
                            <button
                                onClick={doSnapshot}
                                disabled={snapshotBusy}
                                className="shrink-0 rounded-md bg-indigo-500 px-2 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                            >
                                {d.snapshotCreate}
                            </button>
                        </div>
                        {snapshots.length === 0 ? (
                            <p className="mt-3 text-xs text-zinc-400">{d.noSnapshots}</p>
                        ) : (
                            <div className="mt-3 max-h-48 space-y-1 overflow-y-auto">
                                {snapshots.map((s) => (
                                    <div
                                        key={s.version_num}
                                        className="flex items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-zinc-50 dark:hover:bg-zinc-800/60"
                                    >
                                        <div className="min-w-0">
                                            <span className="font-mono font-medium">v{s.version_num}</span>
                                            {s.snapshot_label && (
                                                <span className="ml-1.5 line-clamp-1 text-zinc-500 dark:text-zinc-400">
                                                    {s.snapshot_label}
                                                </span>
                                            )}
                                            <p className="text-zinc-400">{relTime(s.created_at, lang)}</p>
                                        </div>
                                        <button
                                            disabled={busy}
                                            onClick={() => doRestore(s.version_num)}
                                            className="shrink-0 rounded-lg p-1 text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-600 disabled:opacity-40 dark:hover:bg-zinc-800"
                                            title={d.restore}
                                        >
                                            <RotateCcw size={16} />
                                        </button>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>

                    {/* #116: maintenance — auto compact / safety check */}
                    <div className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <Wrench size={16} className="text-zinc-400" />
                            {d.maintenance}
                        </h2>
                        <div className="flex items-center gap-1">
                            <input
                                value={compactBudget}
                                onChange={(e) => setCompactBudget(e.target.value)}
                                placeholder={d.compactBudgetPh}
                                inputMode="numeric"
                                className="min-w-0 flex-1 rounded-md border border-zinc-200/80 bg-white px-2 py-1.5 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                            />
                            <button
                                onClick={doCompact}
                                disabled={maintBusy || !parseInt(compactBudget, 10)}
                                className="shrink-0 rounded-md bg-indigo-500/10 px-2 py-1.5 text-xs font-medium text-indigo-600 transition hover:bg-indigo-500/20 disabled:opacity-40 dark:text-indigo-400"
                            >
                                {d.compactRun}
                            </button>
                        </div>
                        {compactNote && (
                            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">{compactNote}</p>
                        )}
                        <div className="mt-3 flex items-center justify-between gap-2">
                            <span className="flex items-center gap-1 text-xs font-medium text-zinc-500 dark:text-zinc-400">
                                <ShieldCheck size={16} />
                                {d.safetyCheck}
                            </span>
                            <button
                                onClick={doSafety}
                                disabled={maintBusy}
                                className="rounded-md bg-indigo-500/10 px-2 py-1.5 text-xs font-medium text-indigo-600 transition hover:bg-indigo-500/20 disabled:opacity-40 dark:text-indigo-400"
                            >
                                {d.safetyRun}
                            </button>
                        </div>
                        {safetyNote && (
                            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">{safetyNote}</p>
                        )}
                        {codeExported && (
                            <p className="mt-2 truncate text-xs text-zinc-400">
                                {d.exportCode}: {codeExported.length} chars
                            </p>
                        )}
                    </div>

                    {/* #119: review comments attached to the viewed version */}
                    <div className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <MessageCircle size={16} className="text-zinc-400" />
                            {d.comments} ({comments.length})
                        </h2>
                        <textarea
                            value={commentInput}
                            onChange={(e) => setCommentInput(e.target.value)}
                            placeholder={d.commentPh}
                            rows={2}
                            className="w-full resize-none rounded-md border border-zinc-200/80 bg-white px-3 py-2 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                        />
                        <button
                            onClick={doAddComment}
                            disabled={commentBusy || !commentInput.trim()}
                            className="mt-2 w-full rounded-md bg-indigo-500 px-3 py-2 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                        >
                            {d.addComment}
                        </button>
                        {comments.length === 0 ? (
                            <p className="mt-3 text-xs text-zinc-400">{d.noComments}</p>
                        ) : (
                            <div className="mt-3 max-h-48 space-y-2 overflow-y-auto">
                                {comments.map((cm) => (
                                    <div
                                        key={cm.event_id}
                                        className="rounded-md border border-zinc-100 bg-zinc-50/60 px-3 py-2 text-xs dark:border-zinc-800 dark:bg-zinc-950/60"
                                    >
                                        <p className="flex items-center justify-between text-zinc-400">
                                            <span className="font-mono">v{cm.version}</span>
                                            <span>
                                                {cm.author} · {cm.created_at ? relTime(cm.created_at, lang) : ""}
                                            </span>
                                        </p>
                                        <p className="mt-1 whitespace-pre-wrap text-zinc-600 dark:text-zinc-300">
                                            {cm.comment}
                                        </p>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>

                    <div
                        className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
                        onFocus={openAiPanel}
                    >
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <Sparkles size={16} className="text-indigo-400" />
                            {d.aiCard}
                        </h2>
                        <textarea
                            value={aiPrompt}
                            onChange={(e) => setAiPrompt(e.target.value)}
                            placeholder={d.aiPlaceholder}
                            rows={3}
                            className="w-full resize-none rounded-md border border-zinc-200/80 bg-white px-3 py-2 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 dark:border-zinc-800 dark:bg-zinc-900"
                        />
                        {aiModels.length > 0 && (
                            <select
                                value={aiModel}
                                onChange={(e) => setAiModel(e.target.value)}
                                className="mt-2 w-full rounded-md border border-zinc-200/80 bg-white px-2 py-1.5 text-xs text-zinc-600 outline-none dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300"
                            >
                                <option value="">{d.aiModel}: auto</option>
                                {aiModels.map((m) => (
                                    <option key={m} value={m}>
                                        {m}
                                    </option>
                                ))}
                            </select>
                        )}
                        <button
                            disabled={aiBusy || !aiPrompt.trim()}
                            onClick={doAiIterate}
                            className="mt-2 flex w-full items-center justify-center gap-1 rounded-md bg-indigo-500 px-3 py-2 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                        >
                            <Sparkles size={16} />
                            {aiBusy ? d.aiRunning : d.aiRun}
                        </button>
                    </div>

                    <div className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <History size={16} className="text-zinc-400" />
                            {d.versions} ({versions.length})
                        </h2>
                        <div className="max-h-72 space-y-1 overflow-y-auto">
                            {[...versions]
                                .sort((x, y) => y.version_num - x.version_num)
                                .map((v) => (
                                    <div
                                        key={v.version_num}
                                        className={`flex items-center justify-between rounded-md px-2 py-2 text-xs ${
                                            v.version_num === current
                                                ? "bg-indigo-500/10"
                                                : "hover:bg-zinc-50 dark:hover:bg-zinc-800/60"
                                        }`}
                                    >
                                        <div className="min-w-0">
                                            <span className="font-mono font-medium">
                                                v{v.version_num}
                                            </span>
                                            {v.version_num === current && (
                                                <span className="ml-1.5 text-indigo-500">
                                                    {d.current}
                                                </span>
                                            )}
                                            <p className="line-clamp-1 text-zinc-400">
                                                {relTime(v.created_at, lang)} ·{" "}
                                                {v.change_log || v.source} · {fmtBytes(v.size_bytes)}
                                            </p>
                                        </div>
                                        {v.version_num !== current && (
                                            <div className="flex shrink-0 gap-1">
                                                <button
                                                    onClick={() => doDiff(v.version_num)}
                                                    className="rounded-lg p-1 text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-600 dark:hover:bg-zinc-800"
                                                    title={d.compare}
                                                >
                                                    <GitCompare size={16} />
                                                </button>
                                                <button
                                                    disabled={busy}
                                                    onClick={() => doRestore(v.version_num)}
                                                    className="rounded-lg p-1 text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-600 disabled:opacity-40 dark:hover:bg-zinc-800"
                                                    title={d.restore}
                                                >
                                                    <RotateCcw size={16} />
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                ))}
                        </div>
                    </div>

                    <div className="rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                            <Link2 size={16} className="text-zinc-400" />
                            {d.share}
                        </h2>
                        {artifact.share_id ? (
                            <div className="space-y-2">
                                <p className="flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                                    <Check size={16} />
                                    {d.sharedLink}
                                </p>
                                <div className="flex items-center gap-2">
                                    <code className="min-w-0 flex-1 truncate rounded-lg bg-zinc-100 px-2 py-1 font-mono text-xs dark:bg-zinc-800">
                                        {shareUrl}
                                    </code>
                                    <button
                                        onClick={copyLink}
                                        className="rounded-lg border border-zinc-200/80 p-1 text-zinc-500 transition hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
                                        title={d.copy}
                                    >
                                        {copied ? <Check size={16} /> : <Copy size={16} />}
                                    </button>
                                </div>
                                <button
                                    disabled={busy}
                                    onClick={doRevoke}
                                    className="w-full rounded-md border border-red-200 px-3 py-2 text-xs font-medium text-red-600 transition hover:bg-red-50 disabled:opacity-40 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950/50"
                                >
                                    {d.revoke}
                                </button>
                            </div>
                        ) : (
                            <div className="space-y-2">
                                <div className="flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                                    <span className="shrink-0">{d.permission}</span>
                                    <select
                                        value={sharePerm}
                                        onChange={(e) => setSharePerm(e.target.value as typeof sharePerm)}
                                        className="flex-1 rounded-lg border border-zinc-200/80 bg-white px-2 py-1 text-xs outline-none dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                                    >
                                        <option value="view">{d.permView}</option>
                                        <option value="remix">{d.permRemix}</option>
                                    </select>
                                </div>
                                <button
                                    disabled={busy}
                                    onClick={doShare}
                                    className="w-full rounded-md bg-indigo-500 px-3 py-2 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                                >
                                    {d.createShare}
                                </button>
                            </div>
                        )}
                    </div>
                </aside>
            </div>

            {confirm && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/40 p-4">
                    <div className="w-full max-w-sm rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <p className="text-sm leading-relaxed text-zinc-700 dark:text-zinc-200">
                            {confirm.msg}
                        </p>
                        <div className="mt-4 flex justify-end gap-2">
                            <button
                                onClick={() => setConfirm(null)}
                                className="rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                            >
                                {d.close}
                            </button>
                            <button
                                onClick={() => {
                                    const act = confirm.action;
                                    setConfirm(null);
                                    act();
                                }}
                                className="rounded-md bg-red-600 px-3 py-2 text-xs font-medium text-white transition hover:bg-red-700"
                            >
                                {d.confirm}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

function ModeBtn({
    active,
    onClick,
    children,
}: {
    active: boolean;
    onClick: () => void;
    children: React.ReactNode;
}) {
    return (
        <button
            onClick={onClick}
            className={`rounded-md border px-3 py-2 text-xs font-medium transition ${
                active
                    ? "border-indigo-300 bg-indigo-500/10 text-indigo-600 dark:border-indigo-500/50 dark:text-indigo-400"
                    : "border-zinc-200/80 text-zinc-500 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800"
            }`}
        >
            {children}
        </button>
    );
}
