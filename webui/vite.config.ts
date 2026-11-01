import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
    plugins: [react(), tailwindcss()],
    base: "./",
    build: {
        outDir: "../fusion_artifacts_engine/webui_dist",
        emptyOutDir: true,
        sourcemap: false,
    },
    server: {
        proxy: {
            "/api": "http://127.0.0.1:11451",
        },
    },
});
