import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Clock, GitFork, Link2 } from "lucide-react";
import { getPublicShare, remixArtifact, type PublicShare } from "../api";
import { t, type Lang } from "../i18n";

export default function Share({
    id,
    lang,
    onBack,
}: {
    id: string;
    lang: Lang;
    onBack: () => void;
}) {
    const d = t(lang);
    const [data, setData] = useState<PublicShare | null>(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const autoTried = useRef(false);

    useEffect(() => {
        let live = true;
        setData(null);
        setError("");
        getPublicShare(id)
            .then((r) => {
                if (live) setData(r);
            })
            .catch((e) => {
                if (live) setError((e as Error).message);
            });
        return () => {
            live = false;
        };
    }, [id]);

    const doRemix = async () => {
        if (!data?.artifact) return;
        if (!window.confirm(d.confirmRemix)) return;
        setBusy(true);
        try {
            const child = await remixArtifact(data.artifact.id);
            window.location.hash = `#/a/${child.id}`;
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
            setBusy(false);
        }
    };

    useEffect(() => {
        if (!data || autoTried.current) return;
        autoTried.current = true;
        if (
            data.status === "ok" &&
            data.share?.permission === "remix" &&
            window.location.hash.includes("remix=1")
        ) {
            void doRemix();
        }
    }, [data]);

    const back = (
        <button
            onClick={onBack}
            className="flex items-center gap-1.5 rounded-xl border border-zinc-200/80 px-3 py-1.5 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
            <ArrowLeft size={13} />
            {d.back}
        </button>
    );

    if (data && data.status !== "ok") {
        const gone = data.status === "gone";
        return (
            <div className="mx-auto flex h-full w-full max-w-3xl flex-col gap-4 px-6 py-6">
                {back}
                <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-2xl border border-zinc-200/80 bg-white p-10 text-center shadow-sm shadow-zinc-900/5 dark:border-zinc-800 dark:bg-zinc-900">
                    <Clock size={28} className="text-zinc-400" />
                    <p className="text-sm font-semibold">
                        {gone ? d.shareExpired : d.shareNotFound}
                    </p>
                    {gone && data.reason && (
                        <p className="text-xs text-zinc-500 dark:text-zinc-400">{data.reason}</p>
                    )}
                </div>
            </div>
        );
    }

    if (error && !data) {
        return (
            <div className="mx-auto flex h-full w-full max-w-3xl flex-col gap-4 px-6 py-6">
                {back}
                <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
                    {error}
                </p>
            </div>
        );
    }

    if (!data) {
        return (
            <div className="mx-auto flex h-full w-full max-w-3xl items-center justify-center px-6 py-6">
                <p className="text-xs text-zinc-400">{d.loadError}…</p>
            </div>
        );
    }

    const permission = data.share?.permission || "view";
    return (
        <div className="mx-auto flex h-full w-full max-w-6xl flex-col gap-4 px-6 py-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                    {back}
                    <div>
                        <h1 className="text-sm font-semibold leading-tight">{data.artifact?.name}</h1>
                        <p className="flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                            <span className="flex items-center gap-1">
                                <Link2 size={11} />
                                {data.artifact?.type}
                            </span>
                            <span className="rounded-md bg-indigo-500/10 px-1.5 py-0.5 font-medium text-indigo-600 dark:text-indigo-400">
                                {permission === "remix" ? d.permRemix : d.permView}
                            </span>
                        </p>
                    </div>
                </div>
                {permission === "remix" && (
                    <button
                        disabled={busy}
                        onClick={doRemix}
                        className="flex items-center gap-1.5 rounded-xl bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-600 disabled:opacity-40"
                    >
                        <GitFork size={13} />
                        {d.remixShared}
                    </button>
                )}
            </div>
            {error && (
                <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
                    {error}
                </p>
            )}
            <div className="min-h-[420px] flex-1 overflow-hidden rounded-2xl border border-zinc-200/80 bg-white shadow-sm shadow-zinc-900/5 dark:border-zinc-800 dark:bg-zinc-900">
                <iframe
                    sandbox=""
                    srcDoc={data.rendered_html || ""}
                    title={data.artifact?.name || "shared artifact"}
                    className="h-full min-h-[420px] w-full"
                />
            </div>
        </div>
    );
}
