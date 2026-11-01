import { useEffect, useMemo, useState } from "react";
import {
    Code2,
    FileText,
    BarChart3,
    Globe,
    Link2,
    Package,
    RotateCcw,
    Search,
    Star,
    Layers,
} from "lucide-react";
import { listArtifacts, listRecycle, restoreArtifact, type Artifact } from "../api";
import { t, type Lang } from "../i18n";

type TabKey = "all" | "htmlApp" | "document" | "chart" | "code";

const TAB_DEFS: { key: TabKey; match: (a: Artifact) => boolean }[] = [
    { key: "all", match: () => true },
    { key: "htmlApp", match: (a) => a.type === "html" || a.type === "react" },
    { key: "document", match: (a) => a.type === "markdown" },
    { key: "chart", match: (a) => a.type === "data" },
    { key: "code", match: (a) => a.type === "code" },
];

function typeIcon(a: Artifact) {
    switch (a.type) {
        case "html":
        case "react":
            return <Globe size={14} />;
        case "markdown":
            return <FileText size={14} />;
        case "data":
            return <BarChart3 size={14} />;
        default:
            return <Code2 size={14} />;
    }
}

function typeLabel(a: Artifact, lang: Lang) {
    const d = t(lang);
    switch (a.type) {
        case "html":
        case "react":
            return d.htmlApp;
        case "markdown":
            return d.document;
        case "data":
            return d.chart;
        default:
            return d.code;
    }
}

export function relTime(ts: number, lang: Lang): string {
    const d = t(lang);
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return d.justNow;
    if (diff < 3600) return `${Math.floor(diff / 60)} ${d.minutesAgo}`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ${d.hoursAgo}`;
    return `${Math.floor(diff / 86400)} ${d.daysAgo}`;
}

export function sourceLabel(a: Artifact): string {
    if (a.source_module) return a.source_module;
    const meta = a.metadata || {};
    if (typeof meta.source === "string") return meta.source;
    if (typeof meta.session_name === "string") return meta.session_name;
    return a.session_id;
}

function Card({ a, lang, onOpen }: { a: Artifact; lang: Lang; onOpen: (id: string) => void }) {
    const d = t(lang);
    return (
        <button
            onClick={() => onOpen(a.id)}
            className="group flex w-full flex-col gap-3 rounded-2xl border border-zinc-200/80 bg-white p-4 text-left shadow-sm shadow-zinc-900/5 transition hover:border-indigo-300 hover:shadow-md hover:shadow-indigo-500/5 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-500/50"
        >
            <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2 text-sm font-semibold">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                        {typeIcon(a)}
                    </span>
                    <span className="line-clamp-1">{a.name}</span>
                </div>
                {a.is_starred && <Star size={13} className="shrink-0 fill-amber-400 text-amber-400" />}
            </div>
            <div className="flex items-center gap-1.5 text-xs text-zinc-500 dark:text-zinc-400">
                <span className="rounded-md bg-indigo-500/10 px-1.5 py-0.5 font-medium text-indigo-600 dark:text-indigo-400">
                    {typeLabel(a, lang)}
                </span>
                {a.share_id && (
                    <span className="flex items-center gap-0.5 text-emerald-600 dark:text-emerald-400">
                        <Link2 size={11} />
                        {d.sharedLink}
                    </span>
                )}
            </div>
            <div className="flex items-center justify-between text-xs text-zinc-400 dark:text-zinc-500">
                <span className="line-clamp-1">
                    {relTime(a.updated_at, lang)} · {d.source} {sourceLabel(a)}
                </span>
                <span className="flex shrink-0 items-center gap-1">
                    <Layers size={11} />
                    {a.current_version} {d.versionCount}
                </span>
            </div>
        </button>
    );
}

export default function Library({ lang, onOpen }: { lang: Lang; onOpen: (id: string) => void }) {
    const d = t(lang);
    const [artifacts, setArtifacts] = useState<Artifact[]>([]);
    const [recycled, setRecycled] = useState<Artifact[]>([]);
    const [tab, setTab] = useState<TabKey>("all");
    const [query, setQuery] = useState("");
    const [sort, setSort] = useState<"updated_at" | "created_at" | "name">("updated_at");
    const [error, setError] = useState("");
    const [recycleOpen, setRecycleOpen] = useState(false);

    const reload = async () => {
        try {
            const [r, rc] = await Promise.all([listArtifacts(), listRecycle()]);
            setArtifacts(r.artifacts);
            setRecycled(rc.artifacts);
            setError("");
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    useEffect(() => {
        reload();
    }, []);

    const counts = useMemo(() => {
        const c: Record<TabKey, number> = { all: artifacts.length, htmlApp: 0, document: 0, chart: 0, code: 0 };
        for (const def of TAB_DEFS.slice(1)) {
            c[def.key] = artifacts.filter(def.match).length;
        }
        return c;
    }, [artifacts]);

    const shown = useMemo(() => {
        const def = TAB_DEFS.find((x) => x.key === tab)!;
        let list = artifacts.filter(def.match);
        if (query.trim()) {
            const q = query.trim().toLowerCase();
            list = list.filter(
                (a) => a.name.toLowerCase().includes(q) || (a.summary || "").toLowerCase().includes(q)
            );
        }
        const sorted = [...list].sort((x, y) => {
            if (sort === "name") return x.name.localeCompare(y.name);
            return (y[sort] as number) - (x[sort] as number);
        });
        return sorted;
    }, [artifacts, tab, query, sort]);

    const sharedOnes = useMemo(() => artifacts.filter((a) => a.share_id), [artifacts]);

    const tabLabels: Record<TabKey, string> = {
        all: d.all,
        htmlApp: d.htmlApp,
        document: d.document,
        chart: d.chart,
        code: d.code,
    };

    return (
        <div className="mx-auto w-full max-w-5xl px-6 py-6">
            <div className="flex items-center gap-3">
                <div className="relative flex-1">
                    <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-400" />
                    <input
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder={d.search}
                        className="w-full rounded-xl border border-zinc-200/80 bg-white py-2 pl-9 pr-3 text-sm shadow-sm shadow-zinc-900/5 outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-500/10 dark:border-zinc-800 dark:bg-zinc-900"
                    />
                </div>
                <select
                    value={sort}
                    onChange={(e) => setSort(e.target.value as typeof sort)}
                    className="rounded-xl border border-zinc-200/80 bg-white px-3 py-2 text-sm text-zinc-600 shadow-sm shadow-zinc-900/5 outline-none dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300"
                >
                    <option value="updated_at">{d.sortUpdated}</option>
                    <option value="created_at">{d.sortCreated}</option>
                    <option value="name">{d.sortName}</option>
                </select>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
                {TAB_DEFS.map((def) => (
                    <button
                        key={def.key}
                        onClick={() => setTab(def.key)}
                        className={`rounded-xl px-3 py-1.5 text-sm font-medium transition ${
                            tab === def.key
                                ? "bg-indigo-500/10 text-indigo-600 dark:text-indigo-400"
                                : "text-zinc-500 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
                        }`}
                    >
                        {tabLabels[def.key]}{" "}
                        <span className="text-xs text-zinc-400">({counts[def.key]})</span>
                    </button>
                ))}
            </div>

            {error && (
                <p className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
                    {error}
                </p>
            )}

            {shown.length === 0 ? (
                <div className="mt-10 flex flex-col items-center gap-3 text-zinc-400 dark:text-zinc-600">
                    <Package size={36} strokeWidth={1.2} />
                    <p className="text-sm">{d.empty}</p>
                </div>
            ) : (
                <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {shown.map((a) => (
                        <Card key={a.id} a={a} lang={lang} onOpen={onOpen} />
                    ))}
                </div>
            )}

            {sharedOnes.length > 0 && tab === "all" && !query && (
                <section className="mt-8">
                    <h2 className="mb-3 text-sm font-semibold text-zinc-500 dark:text-zinc-400">
                        {d.sharedGroup} ({sharedOnes.length})
                    </h2>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {sharedOnes.map((a) => (
                            <Card key={a.id} a={a} lang={lang} onOpen={onOpen} />
                        ))}
                    </div>
                </section>
            )}

            <section className="mt-8">
                <button
                    onClick={() => setRecycleOpen(!recycleOpen)}
                    className="flex items-center gap-1.5 text-sm font-semibold text-zinc-500 transition hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
                >
                    <RotateCcw size={13} />
                    {d.recycle} ({recycled.length})
                </button>
                {recycleOpen && (
                    recycled.length === 0 ? (
                        <p className="mt-3 text-sm text-zinc-400">{d.recycleEmpty}</p>
                    ) : (
                        <div className="mt-3 flex flex-col gap-2">
                            {recycled.map((a) => (
                                <div
                                    key={a.id}
                                    className="flex items-center justify-between rounded-xl border border-zinc-200/80 bg-white px-4 py-2.5 text-sm dark:border-zinc-800 dark:bg-zinc-900"
                                >
                                    <span className="line-clamp-1 text-zinc-500 dark:text-zinc-400">
                                        {a.name}
                                    </span>
                                    <button
                                        onClick={async () => {
                                            await restoreArtifact(a.id);
                                            reload();
                                        }}
                                        className="shrink-0 rounded-lg bg-indigo-500/10 px-2.5 py-1 text-xs font-medium text-indigo-600 transition hover:bg-indigo-500/20 dark:text-indigo-400"
                                    >
                                        {d.restore}
                                    </button>
                                </div>
                            ))}
                        </div>
                    )
                )}
            </section>
        </div>
    );
}
