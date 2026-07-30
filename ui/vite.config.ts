import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, root, "");
  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": resolve(root),
      },
    },
    server: {
      host: "127.0.0.1",
      proxy: {
        "/api": {
          target:
            environment.REPLAYABLE_API_ORIGIN ?? "http://127.0.0.1:8765",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "out",
      emptyOutDir: true,
    },
  };
});
