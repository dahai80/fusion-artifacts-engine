import { useEffect, useState } from "react";
import { Moon, Package, Sun } from "lucide-react";
import Library from "./components/Library";
import Detail from "./components/Detail";
import Share from "./components/Share";
import type { Lang } from "./i18n";

function useTheme() {
    const [dark, setDark] = useState(() => localStorage.getItem("fae-theme") === "dark");
    useEffect(() => {
        document.documentElement.classList.toggle("dark", dark);
        localStorage.setItem("fae-theme", dark ? "dark" : "light");
    }, [dark]);
    return { dark, toggle: () => setDark((d) => !d) };
}

function parseRoute(): { view: "library" | "detail" | "share"; id?: string } {
    const h = window.location.hash;
    const s = h.match(/^#\/s\/([^?]+)/);
    if (s) return { view: "share", id: decodeURIComponent(s[1]) };
    const m = h.match(/^#\/a\/([^?]+)/);
    if (m) return { view: "detail", id: decodeURIComponent(m[1]) };
    return { view: "library" };
}

export default function App() {
    const theme = useTheme();
    const [lang, setLang] = useState<Lang>(
        () => (localStorage.getItem("fae-lang") as Lang) || "zh"
    );
    const [route, setRoute] = useState(parseRoute);

    useEffect(() => {
        const onHash = () => setRoute(parseRoute());
        window.addEventListener("hashchange", onHash);
        return () => window.removeEventListener("hashchange", onHash);
    }, []);

    useEffect(() => {
        localStorage.setItem("fae-lang", lang);
    }, [lang]);

    const openArtifact = (id: string) => {
        window.location.hash = `#/a/${encodeURIComponent(id)}`;
    };
    const backToLibrary = () => {
        window.location.hash = "";
    };

    return (
        <div className="flex h-full flex-col">
            <header className="flex items-center justify-between border-b border-zinc-200/80 bg-white/80 px-6 py-3 shadow-sm shadow-zinc-900/5 backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/80">
                <div className="flex items-center gap-2.5">
                    <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-indigo-500/10 text-indigo-500">
                        <Package size={18} />
                    </div>
                    <div>
                        <h1 className="text-base font-semibold leading-tight">Artifacts</h1>
                        <p className="text-xs text-zinc-500 dark:text-zinc-400">Fusion</p>
                    </div>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        onClick={() => setLang(lang === "zh" ? "en" : "zh")}
                        className="rounded-xl border border-zinc-200/80 px-3 py-1.5 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                    >
                        {lang === "zh" ? "EN" : "中文"}
                    </button>
                    <button
                        onClick={theme.toggle}
                        aria-label="theme"
                        className="rounded-xl border border-zinc-200/80 p-2 text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                    >
                        {theme.dark ? <Sun size={14} /> : <Moon size={14} />}
                    </button>
                </div>
            </header>
            <main className="flex-1 overflow-y-auto">
                {route.view === "share" && route.id ? (
                    <Share key={route.id} id={route.id} lang={lang} onBack={backToLibrary} />
                ) : route.view === "detail" && route.id ? (
                    <Detail key={route.id} id={route.id} lang={lang} onBack={backToLibrary} />
                ) : (
                    <Library lang={lang} onOpen={openArtifact} />
                )}
            </main>
        </div>
    );
}
