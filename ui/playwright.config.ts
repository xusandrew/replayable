import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8765",
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "pnpm build && uv run replayable ui --cassette-root ../tests/fixtures/cassettes --static-dir out --port 8765",
    cwd: ".",
    url: "http://127.0.0.1:8765",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
