// Playwright config for the predictive-local-echo browser verification.
// Only picks up *.browser.spec.ts (vitest owns the rest). Starts Vite to serve
// the harness page; OMNIGENT_URL points at a dummy local origin so the dev
// proxy's auth path stays inert (the harness needs no backend).

import { defineConfig, devices } from "@playwright/test";

const PORT = 5234;

export default defineConfig({
  testDir: "./src",
  testMatch: "**/*.browser.spec.ts",
  fullyParallel: false,
  // Vite's first compile of the harness on a cold start can race navigation;
  // a single retry absorbs that without masking a real feature regression
  // (the assertions themselves are deterministic once the page loads).
  retries: 2,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "off",
    // Generous nav timeout: the first goto triggers Vite's on-demand compile
    // of the harness module graph (xterm + addon), which is slow cold.
    navigationTimeout: 30_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npx vite --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}/`,
    reuseExistingServer: true,
    timeout: 120_000,
    env: { OMNIGENT_URL: "http://localhost:6767" },
  },
});
