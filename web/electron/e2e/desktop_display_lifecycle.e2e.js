// Desktop-shell harness lane: a lane that only closes Electron must still
// release the Xvfb the harness started for it (headless Linux, no DISPLAY).
//
// Run from web/electron: node --test e2e/desktop_display_lifecycle.e2e.js
// Skips without electron, playwright, or the Xvfb executable.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  launchDesktop,
  pollUntil,
  displaySocketPath,
  xvfbAvailable,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const hasXvfb = xvfbAvailable();
const missing = [...deps.missing, ...(hasXvfb ? [] : ["Xvfb on Linux"])];

describe(
  "desktop shell — harness-owned display lifecycle",
  { skip: missing.length === 0 ? false : `missing deps: ${missing.join(", ")}` },
  () => {
    let savedDisplay;
    let recordDir;

    before(() => {
      savedDisplay = process.env.DISPLAY;
      // Force the headless path even under an xvfb-run wrapper.
      delete process.env.DISPLAY;
      recordDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-display-lifecycle-"));
    });

    after(() => {
      if (savedDisplay === undefined) delete process.env.DISPLAY;
      else process.env.DISPLAY = savedDisplay;
      fs.rmSync(recordDir, { recursive: true, force: true });
    });

    it("releases the owned Xvfb when the app closes without stopDisplayCapture", async () => {
      // No server URL: the shell boots to its bundled setup page.
      const app = await launchDesktop({ recordDir });
      try {
        assert.match(app.display ?? "", /^:\d+$/, "harness did not provide an owned display");
        const socket = displaySocketPath(app.display);
        assert.ok(fs.existsSync(socket), `no X socket at ${socket}`);
        await app.electronApp.close();
        assert.ok(
          await pollUntil(() => !fs.existsSync(socket), 15_000),
          `X socket left at ${socket}`,
        );
      } finally {
        await app.electronApp.close().catch(() => {});
        await app.stopDisplayCapture();
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
    });
  },
);
