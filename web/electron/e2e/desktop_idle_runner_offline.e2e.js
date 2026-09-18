// A session whose runner was reaped by the idle watchdog must surface as
// offline with a reconnect affordance when the user returns.
//
// Journey: connect the desktop app to a server with a live runner-bound
// conversation -> leave it idle past the runner idle timeout (compressed here
// to a few seconds to stand in for the real window) so the runner's own
// watchdog reaps it -> open the app the "next morning" and view the
// conversation -> the session is offline ("Agent disconnected - click to
// reconnect"). The regression guard that the *default* window is long enough
// to survive overnight lives in
// tests/runner/test_runner_idle_default_overnight.py; the live reap journey
// in tests/e2e/test_runner_idle_reaping.py.
//
// Run from web/electron (after building the SPA):
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_idle_runner_offline.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "idle-runner-offline");
// Short enough to observe without an hour-long wait; long enough that the
// runner reaches "online" before the watchdog fires.
const IDLE_TIMEOUT_S = 20;

describe(
  "desktop shell — session reaped after idle is offline the next morning",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir, { idleTimeoutS: IDLE_TIMEOUT_S });
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("shows the conversation offline after the runner idle-reaps", async () => {
      const convId = server.createConversation();

      // Leave the session idle with no client attached; the runner's watchdog
      // reaps it. The server then reports the runner offline.
      const wentOffline = await server.waitForRunnerOffline(90_000);
      assert.equal(
        wentOffline,
        true,
        `runner still online after > 90s idle (configured window ${IDLE_TIMEOUT_S}s)`,
      );

      // The "next morning": open the app and view the conversation.
      const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-data-"));
      const { electronApp, window, stopDisplayCapture } = await launchDesktop({
        recordDir: RECORD_DIR,
        serverUrl: server.serverUrl,
        userDataDir,
      });
      let disconnectedVisible = false;
      try {
        const landing = window.getByTestId("new-chat-landing-input");
        try {
          await landing.waitFor({ state: "visible", timeout: 30_000 });
        } catch {
          // A transient failure of the very first load parks the window on an
          // error surface with nothing to auto-retry; navigate once more.
          await window.goto(server.serverUrl);
          await landing.waitFor({ state: "visible", timeout: 30_000 });
        }
        await window.reload();
        const link = window.locator(`a[href*="/c/${convId}"]`).first();
        await link.waitFor({ state: "visible", timeout: 30_000 });
        await link.click();
        await window.waitForURL(new RegExp(`/c/${convId}([/?#]|$)`), { timeout: 30_000 });
        // The reaped session surfaces the reconnect affordance.
        await window
          .getByTestId("disconnected-indicator")
          .waitFor({ state: "visible", timeout: 30_000 });
        disconnectedVisible = true;
        // Hold the offline state on screen so the recording shows it.
        await window.waitForTimeout(2_500);
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        saveRecording(RECORD_DIR, "idle-offline");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.equal(
        disconnectedVisible,
        true,
        `conversation /c/${convId} did not surface the offline/reconnect indicator after the runner was reaped`,
      );
    });
  },
);
