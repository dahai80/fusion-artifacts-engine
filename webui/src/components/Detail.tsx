import { useEffect, useMemo, useState } from "react";
import { marked } from "marked";
import {
    ArrowLeft,
    Check,
    Copy,
    GitCompare,
    History,
    Link2,
    MessageSquare,
    MessagesSquare,
    Pencil,
    RotateCcw,
    Save,
    Sparkles,
    X,
    GitFork,
} from "lucide-react";
import {
    aiGenerate,
    aiListModels,
    createShare,
    getArtifact,
    getContent,
    getShareBase,
    listEvents,
    listVersions,
    remixArtifact,
    revokeShare,
    rollback,
    updateArtifact,
    versionDiff,
    type Artifact,
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
}: {
    id: string;
    lang: Lang;
    onBack: () => void;
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
    // S6: 禁原生 confirm——阻断确认走自实现 Modal（ConfirmDialog 组件就绪前过渡）
    const [confirm, setConfirm] = useState<{ msg: string; action: () => void } | null>(null);
    // #96: 内容编辑器 + 乐观锁（expected_content_hash，冲突 -32002 显式报错）
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState("");
    const [saving, setSaving] = useState(false);
    // #97: AI 迭代面板（ai.generate → artifact.update source=ai_generation）
    const [aiPrompt, setAiPrompt] = useState("");
    const [aiBusy, setAiBusy] = useState(false);
    const [aiModels, setAiModels] = useState<string[]>([]);
    const [aiModel, setAiModel] = useState("");

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
            const c = await getContent(id);
            setContent(c);
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

    const blobUrl = useMemo(() => {
        if (!artifact || !content) return "";
        const ty = artifact.type;
        if (ty !== "html" && ty !== "react" && ty !== "svg" && ty !== "mermaid") return "";
        // #100-1: mermaid 走同一 blob 通道，包一层 mermaid CDN 渲染壳（CDN 允许，图表类既有约定）
        const doc =
            ty === "mermaid"
                ? `<!DOCTYPE html><html><head><meta charset="utf-8"><script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script><style>html,body{margin:0;padding:16px;background:#fff;display:flex;justify-content:center}</style></head><body><pre class="mermaid">${content.replace(/</g, "&lt;")}</pre><script>mermaid.initialize({startOnLoad:true});</script></body></html>`
                : content;
        return URL.createObjectURL(new Blob([doc], { type: "text/html" }));
    }, [artifact, content]);

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
