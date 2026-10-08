// Desktop-shell harness lane: a lane that only closes Electron must still
// release the Xvfb the harness started for it (headless Linux, no DISPLAY).
//
// Run from web/electron: node --test e2e/desktop_display_lifecycle.e2e.js
// Skips without electron, playwright, or the Xvfb executable.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { desktopDepsAvailable, launchDesktop } = require("./desktopHarness");

const deps = desktopDepsAvailable();
const xvfbAvailable =
  process.platform === "linux" &&
  spawnSync("Xvfb", ["-help"], { stdio: "ignore" }).error === undefined;
const missing = [...deps.missing, ...(xvfbAvailable ? [] : ["Xvfb on Linux"])];

/** Poll `predicate` every 50ms until it holds or `timeoutMs` passes. */
async function waitFor(predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  // Each probe must follow the previous one and the pause between them.
  /* oxlint-disable no-await-in-loop */
  while (!predicate() && Date.now() < deadline) {
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
  }
  /* oxlint-enable no-await-in-loop */
  return predicate();
}

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
      const socket = `/tmp/.X11-unix/X${app.display.slice(1)}`;
      try {
        assert.ok(fs.existsSync(socket), `no X socket at ${socket}`);
        await app.electronApp.close();
        assert.ok(
          await waitFor(() => !fs.existsSync(socket), 15_000),
          `X socket left at ${socket}`,
        );
      } finally {
        await app.stopDisplayCapture();
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
    });
  },
);
