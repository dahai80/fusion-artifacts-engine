import { useEffect, useState } from "react";
import { Moon, Package, Sun } from "lucide-react";
import Library from "./components/Library";
import Detail from "./components/Detail";
import Share from "./components/Share";
import type { Lang } from "./i18n";

// S7: embedded mode when proxied via gateway /shell/<id>/ or with ?embedded=1 —
// hide our own top nav/theme/language controls (the Shell owns the chrome, §2.1/§4.1)
const EMBEDDED =
    window.location.pathname.includes("/shell/") ||
    new URLSearchParams(window.location.search).has("embedded");

function readShellPref(key: string): string | null {
    try {
        return window.parent.localStorage.getItem(key);
    } catch {
        return null;
    }
}

// §3.1: dark is the default theme; embedded mode follows the Shell (fusion.shell.theme + fusion:theme events).
// Modules must not detect it themselves (bridged via same-origin storage/events until the SDK theme API is ready)
function useTheme() {
    const [dark, setDark] = useState(() => {
        if (EMBEDDED) return readShellPref("fusion.shell.theme") !== "light";
        return localStorage.getItem("fae-theme") !== "light";
    });
    useEffect(() => {
        document.documentElement.classList.toggle("dark", dark);
        document.documentElement.classList.toggle("light", !dark);
        if (!EMBEDDED) localStorage.setItem("fae-theme", dark ? "dark" : "light");
    }, [dark]);
    useEffect(() => {
        if (!EMBEDDED) return;
        const onTheme = (e: Event) => setDark((e as CustomEvent).detail !== "light");
        window.parent.addEventListener("fusion:theme", onTheme);
        return () => window.parent.removeEventListener("fusion:theme", onTheme);
    }, []);
    return { dark, toggle: () => setDark((d) => !d) };
}

// §9.1: UI copy follows the Shell language (fusion.shell.lang + fusion:lang events)
function useLang() {
    const [lang, setLang] = useState<Lang>(() => {
        if (EMBEDDED) return readShellPref("fusion.shell.lang") === "en" ? "en" : "zh";
        return (localStorage.getItem("fae-lang") as Lang) || "zh";
    });
    useEffect(() => {
        if (!EMBEDDED) localStorage.setItem("fae-lang", lang);
    }, [lang]);
    useEffect(() => {
        if (!EMBEDDED) return;
        const onLang = (e: Event) => setLang((e as CustomEvent).detail === "en" ? "en" : "zh");
        window.parent.addEventListener("fusion:lang", onLang);
        return () => window.parent.removeEventListener("fusion:lang", onLang);
    }, []);
    return { lang, setLang };
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
    const { lang, setLang } = useLang();
    const [route, setRoute] = useState(parseRoute);

    useEffect(() => {
        const onHash = () => setRoute(parseRoute());
        window.addEventListener("hashchange", onHash);
        return () => window.removeEventListener("hashchange", onHash);
    }, []);

    const openArtifact = (id: string) => {
        window.location.hash = `#/a/${encodeURIComponent(id)}`;
    };
    const backToLibrary = () => {
        window.location.hash = "";
    };

    return (
        <div className="flex h-full flex-col">
            {!EMBEDDED && (
                <header className="flex items-center justify-between border-b border-zinc-200/80 bg-white/80 px-6 py-3 backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/80">
                    <button
                        onClick={backToLibrary}
                        className="flex items-center gap-3 rounded-md text-left transition hover:opacity-80"
                    >
                        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-500 text-white">
                            <Package size={20} />
                        </div>
                        <div>
                            <h1 className="text-lg font-semibold leading-tight">Artifacts</h1>
                            <p className="text-xs text-zinc-500 dark:text-zinc-400">Fusion</p>
                        </div>
                    </button>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={() => setLang(lang === "zh" ? "en" : "zh")}
                            className="rounded-md border border-zinc-200/80 px-3 py-2 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                        >
                            {lang === "zh" ? "EN" : "中文"}
                        </button>
                        <button
                            onClick={theme.toggle}
                            aria-label="theme"
                            className="rounded-md border border-zinc-200/80 p-2 text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                        >
                            {theme.dark ? <Sun size={16} /> : <Moon size={16} />}
                        </button>
                    </div>
                </header>
            )}
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
