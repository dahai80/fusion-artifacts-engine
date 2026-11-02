import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import {
    BarChart3,
    Code2,
    FileText,
    GitFork,
    Globe,
    Layers,
    Link2,
    MessageSquare,
    Package,
    RotateCcw,
    Search,
    Star,
} from "lucide-react";
import { getContent, listArtifacts, listRecycle, restoreArtifact, type Artifact } from "../api";
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

export function sourceLabel(a: Artifact, lang: Lang = "zh"): string {
    const d = t(lang);
    const raw = a.source_module || (a.metadata || {}).source;
    if (raw === "fusion-design") return d.sourceDesign;
    if (a.source_module) return a.source_module;
    const meta = a.metadata || {};
    if (typeof meta.source === "string") return meta.source;
    if (typeof meta.session_name === "string") return meta.session_name;
    return a.session_id;
}

const contentCache = new Map<string, Promise<string>>();

function fetchContent(id: string): Promise<string> {
    let p = contentCache.get(id);
    if (!p) {
        p = getContent(id).catch((e) => {
            contentCache.delete(id);
            throw e;
        });
        contentCache.set(id, p);
    }
    return p;
}

function useInView() {
    const ref = useRef<HTMLDivElement | null>(null);
    const [seen, setSeen] = useState(false);
    useEffect(() => {
        const el = ref.current;
        if (!el || seen) return;
        const obs = new IntersectionObserver(
            (entries) => {
                if (entries.some((e) => e.isIntersecting)) {
                    setSeen(true);
                    obs.disconnect();
                }
            },
            { rootMargin: "300px" }
        );
        obs.observe(el);
        return () => obs.disconnect();
    }, [seen]);
    return { ref, seen };
}

function Thumb({ a }: { a: Artifact }) {
    const { ref, seen } = useInView();
    const [srcDoc, setSrcDoc] = useState<string | null>(null);
    const [text, setText] = useState<string | null>(null);
    const visual = a.type === "html" || a.type === "react" || a.type === "svg";

    useEffect(() => {
        if (!seen) return;
        let alive = true;
        fetchContent(a.id)
            .then(async (c) => {
                if (!alive) return;
                if (a.type === "markdown") {
                    const html = marked.parse(c, { async: false }) as string;
                    setSrcDoc(
                        `<!DOCTYPE html><html><head><style>body{font-family:system-ui,sans-serif;padding:10px;line-height:1.55;font-size:15px}pre{background:#f4f4f4;padding:6px;border-radius:6px;overflow:hidden}code{background:#f4f4f4;padding:1px 4px;border-radius:4px}img{max-width:100%}</style></head><body>${html}</body></html>`
                    );
                } else if (visual) {
                    setSrcDoc(c);
                } else {
                    setText(c.slice(0, 600));
                }
            })
            .catch(() => {});
        return () => {
            alive = false;
        };
    }, [seen, a.id, a.type, visual]);

    return (
        <div
            ref={ref}
            className="relative aspect-video w-full overflow-hidden rounded-xl border border-zinc-100 bg-white dark:border-zinc-800 dark:bg-zinc-950"
        >
            {srcDoc !== null ? (
                <iframe
                    title={a.name}
                    sandbox=""
                    srcDoc={srcDoc}
                    className="pointer-events-none absolute left-0 top-0 h-[200%] w-[200%] origin-top-left border-0"
                />
            ) : text !== null ? (
                <pre className="h-full w-full overflow-hidden whitespace-pre-wrap break-all p-3 text-[10px] leading-tight text-zinc-500 dark:text-zinc-400">
                    {text}
                </pre>
            ) : (
                <div className="flex h-full items-center justify-center text-zinc-300 dark:text-zinc-700">
                    {typeIcon(a)}
                </div>
            )}
        </div>
    );
}

function Card({ a, lang, onOpen }: { a: Artifact; lang: Lang; onOpen: (id: string) => void }) {
    const d = t(lang);
    return (
        <button
            onClick={() => onOpen(a.id)}
            className="group flex w-full flex-col gap-3 rounded-2xl border border-zinc-200/80 bg-white p-4 text-left shadow-sm shadow-zinc-900/5 transition hover:border-indigo-300 hover:shadow-md hover:shadow-indigo-500/5 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-500/50"
        >
            <Thumb a={a} />
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
                {a.parent_artifact_id && (
                    <span className="flex items-center gap-0.5 text-violet-600 dark:text-violet-400">
                        <GitFork size={11} />
                        {d.remixBadge}
                    </span>
                )}
                {a.share_id && (
                    <span className="flex items-center gap-0.5 text-emerald-600 dark:text-emerald-400">
                        <Link2 size={11} />
                        {d.sharedLink}
                    </span>
                )}
            </div>
            <div className="flex items-center justify-between text-xs text-zinc-400 dark:text-zinc-500">
                <span className="line-clamp-1">
                    {relTime(a.updated_at, lang)} · {d.source} {sourceLabel(a, lang)}
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
        return list;
    }, [artifacts, tab, query]);

    // claude.ai gallery style: group by originating conversation, groups by
    // most recent update, items within a group by the chosen sort
    const groups = useMemo(() => {
        const map = new Map<string, Artifact[]>();
        for (const a of shown) {
            const list = map.get(a.session_id);
            if (list) list.push(a);
            else map.set(a.session_id, [a]);
        }
        const entries = [...map.entries()];
        entries.sort(
            (x, y) =>
                Math.max(...y[1].map((a) => a.updated_at)) - Math.max(...x[1].map((a) => a.updated_at))
        );
        for (const [, items] of entries) {
            items.sort((x, y) => {
                if (sort === "name") return x.name.localeCompare(y.name);
                return (y[sort] as number) - (x[sort] as number);
            });
        }
        return entries;
    }, [shown, sort]);

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
                <div className="mt-5 flex flex-col gap-7">
                    {groups.map(([sid, items]) => (
                        <section key={sid}>
                            <h2 className="mb-2.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
                                <MessageSquare size={12} className="shrink-0" />
                                <span className="line-clamp-1 normal-case tracking-normal">
                                    {sourceLabel(items[0], lang)}
                                </span>
                                <span className="font-normal">
                                    · {items.length} · {relTime(Math.max(...items.map((a) => a.updated_at)), lang)}
                                </span>
                            </h2>
                            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                                {items.map((a) => (
                                    <Card key={a.id} a={a} lang={lang} onOpen={onOpen} />
                                ))}
                            </div>
                        </section>
                    ))}
                </div>
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
