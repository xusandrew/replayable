import { defineConfig } from "vitest/config";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      "@": resolve(root),
    },
  },
  test: {
    environment: "jsdom",
    // Anything ending in .test.ts(x) outside the Playwright suite runs. The
    // previous `components/**/*.test.tsx` glob silently skipped every test
    // under lib/, which is where the pure logic lives.
    include: ["{components,lib}/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
