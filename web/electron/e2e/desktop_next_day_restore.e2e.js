// Relaunching the desktop app must return to the conversation the user left
// open, not kick them to the home page.
//
// Journey: connect the desktop app to a server -> open a conversation and view
// it -> quit the app -> relaunch it (the "next morning") with the same profile
// -> the window must restore that conversation. Without last-route
// persistence the shell reloads the saved server URL's root, so the SPA
// renders the new-chat home page and the user is dropped away from where
// they left off.
//
// Run from web/electron (after building the SPA):
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_next_day_restore.e2e.js

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
const RECORD_ROOT = path.join(__dirname, "recordings", "next-day-restore");
const DAY1_DIR = path.join(RECORD_ROOT, "day1");
const DAY2_DIR = path.join(RECORD_ROOT, "day2");

describe(
  "desktop shell — next-day relaunch returns to the conversation",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir);
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("reopens on the conversation left open, not the home page", async () => {
      // A real conversation the user will open (created via the same API path
      // tests/e2e_ui/conftest.py uses to seed a runnable session).
      const convId = server.createConversation();
      // One persistent profile for both launches, like a real installation.
      const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-data-"));

      // Day 1: open the conversation from the sidebar and view it, then quit.
      {
        const { electronApp, window, stopDisplayCapture } = await launchDesktop({
          recordDir: DAY1_DIR,
          serverUrl: server.serverUrl,
          userDataDir,
        });
        try {
          await window
            .getByTestId("new-chat-landing-input")
            .waitFor({ state: "visible", timeout: 60_000 });
          // Reload so the sidebar lists the just-created session, then click it.
          await window.reload();
          const link = window.locator(`a[href*="/c/${convId}"]`).first();
          await link.waitFor({ state: "visible", timeout: 30_000 });
          await link.click();
          await window.waitForURL(new RegExp(`/c/${convId}([/?#]|$)`), { timeout: 30_000 });
          await window.waitForTimeout(1_500);
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saveRecording(DAY1_DIR, "day1-open");
        }
      }

      // Day 2: relaunch with the same profile (saved settings intact).
      let outcome;
      let landedUrl;
      {
        const { electronApp, window, stopDisplayCapture } = await launchDesktop({
          recordDir: DAY2_DIR,
          userDataDir,
        });
        try {
          const landing = window.getByTestId("new-chat-landing-input");
          outcome = await Promise.race([
            window
              .waitForURL(new RegExp(`/c/${convId}([/?#]|$)`), { timeout: 30_000 })
              .then(() => "restored"),
            landing.waitFor({ state: "visible", timeout: 30_000 }).then(() => "home"),
          ]);
          landedUrl = window.url();
          // Hold the landed state on screen so the recording shows it.
          await window.waitForTimeout(2_500);
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saveRecording(DAY2_DIR, "relaunch");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
      }
      assert.equal(
        outcome,
        "restored",
        `next-day relaunch landed on ${landedUrl} (the home page) instead of restoring /c/${convId}`,
      );
    });
  },
);
