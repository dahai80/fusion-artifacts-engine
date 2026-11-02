import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import {
    BarChart3,
    Clock3,
    Code2,
    FileText,
    GitFork,
    Globe,
    Layers,
    Link2,
    PackageOpen,
    Palette,
    Presentation,
    RotateCcw,
    Search,
    Star,
} from "lucide-react";
import {
    createArtifact,
    getContent,
    listArtifacts,
    listRecycle,
    restoreArtifact,
    type Artifact,
} from "../api";
import { t, type Lang } from "../i18n";

type TabKey = "all" | "htmlApp" | "document" | "chart" | "code";
type TimeBucket = "today" | "yesterday" | "week" | "earlier";

const TAB_DEFS: { key: TabKey; match: (a: Artifact) => boolean }[] = [
    { key: "all", match: () => true },
    { key: "htmlApp", match: (a) => a.type === "html" || a.type === "react" },
    { key: "document", match: (a) => a.type === "markdown" },
    { key: "chart", match: (a) => a.type === "data" },
    { key: "code", match: (a) => a.type === "code" },
];

// 分类色为低饱和 tint（非 §3.1 状态语义色，不与 success/running/warning/error 冲突）；
// @fusion/web-ui preset 就绪前以 zinc/indigo + 类别 tint 过渡（§12.3）
const TYPE_TILE: Record<string, string> = {
    html: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
    react: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
    markdown: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
    data: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    code: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
};

function typeIcon(a: Artifact, size = 16) {
    switch (a.type) {
        case "html":
        case "react":
            return <Globe size={size} />;
        case "markdown":
            return <FileText size={size} />;
        case "data":
            return <BarChart3 size={size} />;
        default:
            return <Code2 size={size} />;
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
                        `<!DOCTYPE html><html><head><style>body{font-family:system-ui,sans-serif;padding:12px;line-height:1.55;font-size:14px}pre{background:#f4f4f4;padding:8px;border-radius:4px;overflow:hidden}code{background:#f4f4f4;padding:1px 4px;border-radius:4px}img{max-width:100%}</style></head><body>${html}</body></html>`
                    );
                } else if (visual) {
                    setSrcDoc(c);
                } else {
                    const head = c.trim().slice(0, 1);
                    if (head === "{" || head === "[") {
                        setText(null);
                    } else {
                        setText(c.slice(0, 600));
                    }
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
            className="relative aspect-video w-full overflow-hidden rounded-md border border-zinc-100 bg-white dark:border-zinc-800 dark:bg-zinc-950"
        >
            {srcDoc !== null ? (
                <iframe
                    title={a.name}
                    sandbox=""
                    srcDoc={srcDoc}
                    className="pointer-events-none absolute left-0 top-0 h-[200%] w-[200%] origin-top-left border-0"
                />
            ) : text !== null ? (
                <pre className="h-full w-full overflow-hidden whitespace-pre-wrap break-all p-3 text-xs leading-tight text-zinc-500 dark:text-zinc-400">
                    {text}
                </pre>
            ) : (
                <div
                    className={`flex h-full items-center justify-center ${
                        TYPE_TILE[a.type] || "bg-zinc-100 text-zinc-400 dark:bg-zinc-800/60 dark:text-zinc-600"
                    }`}
                >
                    {typeIcon(a, 20)}
                </div>
            )}
        </div>
    );
}

// §3.3: 平面卡片用边框不用阴影；rounded-lg 卡片 + rounded-md 内嵌元素
function Card({ a, lang, onOpen }: { a: Artifact; lang: Lang; onOpen: (id: string) => void }) {
    const d = t(lang);
    const tile = TYPE_TILE[a.type] || "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400";
    return (
        <button
            onClick={() => onOpen(a.id)}
            className="group flex w-full flex-col gap-3 rounded-lg border border-zinc-200/80 bg-white p-4 text-left transition duration-150 hover:border-indigo-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-500/50"
        >
            <Thumb a={a} />
            <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2 text-sm font-semibold">
                    <span
                        className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md ${tile}`}
                    >
                        {typeIcon(a)}
                    </span>
                    <span className="line-clamp-1">{a.name}</span>
                </div>
                {a.is_starred && <Star size={16} className="shrink-0 fill-amber-400 text-amber-400" />}
            </div>
            {a.summary && !/^\s*[[{]/.test(a.summary) && (
                <p className="line-clamp-2 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
                    {a.summary}
                </p>
            )}
            <div className="mt-auto flex items-center justify-between gap-2 text-xs text-zinc-400 dark:text-zinc-500">
                <div className="flex min-w-0 items-center gap-2">
                    <span
                        className={`shrink-0 rounded-full px-2 py-0.5 font-medium ${tile}`}
                    >
                        {typeLabel(a, lang)}
                    </span>
                    {a.parent_artifact_id && (
                        <span className="flex shrink-0 items-center gap-0.5 text-violet-600 dark:text-violet-400">
                            <GitFork size={16} />
                            {d.remixBadge}
                        </span>
                    )}
                    {a.share_id && (
                        <span className="flex shrink-0 items-center gap-0.5 text-emerald-600 dark:text-emerald-400">
                            <Link2 size={16} />
                            {d.sharedLink}
                        </span>
                    )}
                </div>
                <span className="flex shrink-0 items-center gap-1">
                    <Layers size={16} />
                    v{a.current_version}
                </span>
            </div>
            <div className="flex items-center justify-between border-t border-zinc-100 pt-3 text-xs text-zinc-400 dark:border-zinc-800 dark:text-zinc-500">
                <span className="line-clamp-1">{sourceLabel(a, lang)}</span>
                <span className="shrink-0">{relTime(a.updated_at, lang)}</span>
            </div>
        </button>
    );
}

function Skeleton() {
    return (
        <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2, 3, 4, 5].map((i) => (
                <div
                    key={i}
                    className="animate-pulse rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
                >
                    <div className="aspect-video w-full rounded-md bg-zinc-100 dark:bg-zinc-800" />
                    <div className="mt-3 h-4 w-2/3 rounded bg-zinc-100 dark:bg-zinc-800" />
                    <div className="mt-2 h-3 w-1/2 rounded bg-zinc-100 dark:bg-zinc-800" />
                </div>
            ))}
        </div>
    );
}

function timeBucket(ts: number): TimeBucket {
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    if (ts >= startToday) return "today";
    if (ts >= startToday - 86400) return "yesterday";
    if (ts >= startToday - 7 * 86400) return "week";
    return "earlier";
}

const BUCKET_ORDER: TimeBucket[] = ["today", "yesterday", "week", "earlier"];

// 新建模板：类型映射 文档=markdown / PPT=html(自包含幻灯) / 设计=data(JSON 画布)
const DOC_TEMPLATE = `# 未命名文档

在这里开始写正文。

## 第一节

- 要点一
- 要点二
`;

const PPT_TEMPLATE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#18181b;color:#fafafa;overflow:hidden}
.slide{display:none;width:100vw;height:100vh;padding:80px 96px;flex-direction:column;justify-content:center;gap:24px}
.slide.active{display:flex}
h1{font-size:56px;letter-spacing:-0.02em}
h2{font-size:40px;letter-spacing:-0.02em}
p,li{font-size:22px;line-height:1.6;color:#a1a1aa}
ul{padding-left:28px;display:flex;flex-direction:column;gap:12px}
.page{position:fixed;right:32px;bottom:24px;font-size:14px;color:#52525b}
</style>
</head>
<body>
<section class="slide active"><h1>未命名演示</h1><p>点击或按方向键翻页</p></section>
<section class="slide"><h2>要点页</h2><ul><li>要点一</li><li>要点二</li><li>要点三</li></ul></section>
<section class="slide"><h2>结束页</h2><p>谢谢观看</p></section>
<div class="page" id="page"></div>
<script>
var s=document.querySelectorAll('.slide'),i=0;
function show(n){i=(n+s.length)%s.length;for(var k=0;k<s.length;k++)s[k].classList.toggle('active',k===i);document.getElementById('page').textContent=(i+1)+' / '+s.length}
document.addEventListener('click',function(){show(i+1)});
document.addEventListener('keydown',function(e){if(e.key==='ArrowRight'||e.key===' ')show(i+1);if(e.key==='ArrowLeft')show(i-1)});
show(0);
</script>
</body>
</html>
`;

const DESIGN_TEMPLATE = JSON.stringify(
    { title: "未命名设计", canvas: { width: 1280, height: 720 }, elements: [] },
    null,
    4
);

export default function Library({ lang, onOpen }: { lang: Lang; onOpen: (id: string) => void }) {
    const d = t(lang);
    const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
    const [recycled, setRecycled] = useState<Artifact[]>([]);
    const [tab, setTab] = useState<TabKey>("all");
    const [query, setQuery] = useState("");
    const [sort, setSort] = useState<"updated_at" | "created_at" | "name">("updated_at");
    const [error, setError] = useState("");
    const [recycleOpen, setRecycleOpen] = useState(false);
    const [creating, setCreating] = useState<string | null>(null);

    const doCreate = async (key: string, name: string, type: string, content: string) => {
        if (creating) return;
        setCreating(key);
        try {
            const art = await createArtifact(name, type, content);
            await reload();
            onOpen(art.id);
        } catch (e) {
            setError(`${d.loadError}: ${(e as Error).message}`);
        } finally {
            setCreating(null);
        }
    };

    const reload = async () => {
        try {
            const [r, rc] = await Promise.all([listArtifacts(), listRecycle()]);
            setArtifacts(r.artifacts);
            setRecycled(rc.artifacts);
            setError("");
        } catch (e) {
            setArtifacts([]);
            setError(`${d.loadError}: ${(e as Error).message}`);
        }
    };

    useEffect(() => {
        reload();
    }, []);

    const counts = useMemo(() => {
        const c: Record<TabKey, number> = { all: 0, htmlApp: 0, document: 0, chart: 0, code: 0 };
        if (!artifacts) return c;
        c.all = artifacts.length;
        for (const def of TAB_DEFS.slice(1)) {
            c[def.key] = artifacts.filter(def.match).length;
        }
        return c;
    }, [artifacts]);

    const shown = useMemo(() => {
        if (!artifacts) return [];
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

    // claude.ai 时间分段（今天/昨天/7天内/更早）替代 v0.6.3 的 session 碎组——
    // session id 对用户不可读，碎组头是主要视觉噪音。名称排序时退化为平铺。
    const sections = useMemo(() => {
        const sorted = [...shown].sort((x, y) => {
            if (sort === "name") return x.name.localeCompare(y.name);
            return (y[sort] as number) - (x[sort] as number);
        });
        if (sort === "name") return [{ bucket: null as TimeBucket | null, items: sorted }];
        const map = new Map<TimeBucket, Artifact[]>();
        for (const a of sorted) {
            const b = timeBucket(sort === "created_at" ? a.created_at : a.updated_at);
            const list = map.get(b);
            if (list) list.push(a);
            else map.set(b, [a]);
        }
        return BUCKET_ORDER.filter((b) => map.has(b)).map((bucket) => ({
            bucket,
            items: map.get(bucket)!,
        }));
    }, [shown, sort]);

    const bucketLabels: Record<TimeBucket, string> = {
        today: d.today,
        yesterday: d.yesterday,
        week: d.thisWeek,
        earlier: d.earlier,
    };

    const totalVersions = useMemo(
        () => (artifacts || []).reduce((n, a) => n + (a.current_version || 0), 0),
        [artifacts]
    );

    const tabLabels: Record<TabKey, string> = {
        all: d.all,
        htmlApp: d.htmlApp,
        document: d.document,
        chart: d.chart,
        code: d.code,
    };

    return (
        <div className="mx-auto w-full max-w-6xl px-6 py-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h2 className="text-lg font-semibold tracking-tight">{d.libraryTitle}</h2>
                    <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
                        {(artifacts || []).length} {d.totalArtifacts} · {totalVersions} {d.totalVersions}
                    </p>
                </div>
            </div>

            <div className="sticky top-0 z-10 mt-4 flex flex-wrap items-center gap-2 border-b border-zinc-200/70 bg-zinc-50/95 py-2 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95">
                <div className="flex rounded-md border border-zinc-200/80 bg-white p-1 dark:border-zinc-800 dark:bg-zinc-900">
                    {TAB_DEFS.map((def) => (
                        <button
                            key={def.key}
                            onClick={() => setTab(def.key)}
                            className={`rounded-md px-3 py-1 text-xs font-medium transition ${
                                tab === def.key
                                    ? "bg-indigo-500 text-white"
                                    : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
                            }`}
                        >
                            {tabLabels[def.key]}
                            <span
                                className={`ml-1 ${
                                    tab === def.key
                                        ? "text-white/70"
                                        : "text-zinc-400 dark:text-zinc-500"
                                }`}
                            >
                                {counts[def.key]}
                            </span>
                        </button>
                    ))}
                </div>
                <div className="ml-auto flex items-center gap-2">
                    <div className="relative w-40 xl:w-48">
                        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-400" />
                        <input
                            value={query}
                            onChange={(e) => setQuery(e.target.value)}
                            placeholder={d.search}
                            className="w-full rounded-md border border-zinc-200/80 bg-white py-2 pl-9 pr-3 text-xs outline-none transition placeholder:text-zinc-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-500/10 dark:border-zinc-800 dark:bg-zinc-900"
                        />
                    </div>
                    <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value as typeof sort)}
                        className="rounded-md border border-zinc-200/80 bg-white px-3 py-2 text-xs text-zinc-600 outline-none dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300"
                    >
                        <option value="updated_at">{d.sortUpdated}</option>
                        <option value="created_at">{d.sortCreated}</option>
                        <option value="name">{d.sortName}</option>
                    </select>
                    <button
                        onClick={() => setRecycleOpen(!recycleOpen)}
                        className={`flex items-center gap-2 rounded-md border px-3 py-2 text-xs font-medium transition ${
                            recycleOpen
                                ? "border-indigo-300 bg-indigo-500/10 text-indigo-600 dark:border-indigo-500/50 dark:text-indigo-400"
                                : "border-zinc-200/80 bg-white text-zinc-500 hover:text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-200"
                        }`}
                    >
                        <RotateCcw size={16} />
                        {d.recycle}
                        {recycled.length > 0 && (
                            <span className="rounded-full bg-zinc-200 px-2 text-xs font-semibold text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300">
                                {recycled.length}
                            </span>
                        )}
                    </button>
                </div>
            </div>

            <div className="mt-4 grid grid-cols-3 gap-4">
                {(
                    [
                        { key: "doc", label: d.newDocument, icon: FileText, tile: "bg-violet-500/10 text-violet-600 dark:text-violet-400", tpl: DOC_TEMPLATE, type: "markdown" },
                        { key: "ppt", label: d.newPPT, icon: Presentation, tile: "bg-sky-500/10 text-sky-600 dark:text-sky-400", tpl: PPT_TEMPLATE, type: "html" },
                        { key: "design", label: d.newDesign, icon: Palette, tile: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400", tpl: DESIGN_TEMPLATE, type: "data" },
                    ] as const
                ).map(({ key, label, icon: Icon, tile, tpl, type }) => (
                    <button
                        key={key}
                        disabled={creating !== null}
                        onClick={() =>
                            doCreate(
                                key,
                                type === "markdown" ? d.untitledDoc : type === "html" ? d.untitledPPT : d.untitledDesign,
                                type,
                                tpl
                            )
                        }
                        className="flex items-center gap-3 rounded-lg border border-dashed border-zinc-300 bg-white px-4 py-3 text-sm font-medium text-zinc-500 transition hover:border-indigo-400 hover:text-indigo-600 disabled:opacity-40 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:border-indigo-500/60 dark:hover:text-indigo-400"
                    >
                        <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${tile}`}>
                            <Icon size={20} />
                        </span>
                        {creating === key ? d.creating : label}
                    </button>
                ))}
            </div>

            {error && (
                <p className="mt-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
                    {error}
                </p>
            )}

            {artifacts === null ? (
                <Skeleton />
            ) : shown.length === 0 ? (
                <div className="mt-16 flex flex-col items-center gap-3 text-zinc-400 dark:text-zinc-600">
                    <div className="flex h-16 w-16 items-center justify-center rounded-lg bg-zinc-100 dark:bg-zinc-800/60">
                        <PackageOpen size={24} strokeWidth={1.4} />
                    </div>
                    <p className="text-sm font-medium text-zinc-500 dark:text-zinc-400">
                        {artifacts.length === 0 ? d.empty : d.searchEmpty}
                    </p>
                    <p className="max-w-xs text-center text-xs text-zinc-400 dark:text-zinc-600">
                        {artifacts.length === 0 ? d.emptyHint : d.searchHint}
                    </p>
                </div>
            ) : (
                <div className="mt-6 flex flex-col gap-8">
                    {sections.map(({ bucket, items }) => (
                        <section key={bucket || "flat"}>
                            {bucket && (
                                <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
                                    <Clock3 size={16} className="shrink-0" />
                                    {bucketLabels[bucket]}
                                    <span className="font-normal normal-case tracking-normal text-zinc-300 dark:text-zinc-600">
                                        · {items.length}
                                    </span>
                                </h2>
                            )}
                            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                                {items.map((a) => (
                                    <Card key={a.id} a={a} lang={lang} onOpen={onOpen} />
                                ))}
                            </div>
                        </section>
                    ))}
                </div>
            )}

            {recycleOpen && (
                <section className="mt-8 rounded-lg border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                    <h3 className="flex items-center gap-2 text-sm font-semibold">
                        <RotateCcw size={16} className="text-zinc-400" />
                        {d.recycle}
                    </h3>
                    {recycled.length === 0 ? (
                        <p className="mt-3 text-sm text-zinc-400">{d.recycleEmpty}</p>
                    ) : (
                        <div className="mt-4 flex flex-col gap-2">
                            {recycled.map((a) => (
                                <div
                                    key={a.id}
                                    className="flex items-center justify-between rounded-md border border-zinc-100 bg-zinc-50/60 px-4 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-950/60"
                                >
                                    <span className="line-clamp-1 text-zinc-500 dark:text-zinc-400">
                                        {a.name}
                                    </span>
                                    <button
                                        onClick={async () => {
                                            await restoreArtifact(a.id);
                                            reload();
                                        }}
                                        className="shrink-0 rounded-md bg-indigo-500/10 px-3 py-1 text-xs font-medium text-indigo-600 transition hover:bg-indigo-500/20 dark:text-indigo-400"
                                    >
                                        {d.restore}
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}
                </section>
            )}
        </div>
    );
}
