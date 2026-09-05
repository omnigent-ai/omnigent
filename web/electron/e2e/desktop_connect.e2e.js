// Example desktop-shell recording lane: the connect journey.
//
// This is the reference test a repro-agent COPIES for a desktop-shell bug
// (see e2e/README.md). It films the real Electron window: launch on the
// bundled setup page, type a server URL, Connect, and land in the app shell.
// The recorded .webm is the desktop journey a user sees — the artifact the
// repro-agent attaches for a `web`/desktop facet.
//
// Run: `node --test e2e/desktop_connect.e2e.js` from web/electron, AFTER
// building the SPA (pnpm --filter web run build). Skips cleanly when electron
// or playwright aren't installed (they're not in the web-test CI path).

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
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-connect");

describe(
  "desktop shell — connect journey",
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

    it("connects from the setup page and lands in the app shell", async () => {
      // Launch WITHOUT a pre-seeded server so the journey starts on the bundled
      // setup page — the connect flow is part of the desktop journey we film.
      const { electronApp, window, userDataDir } = await launchDesktop({ recordDir: RECORD_DIR });
      let saved;
      try {
        // 1. The bundled "connect to server" setup page is shown.
        const urlField = window.locator("#url");
        await urlField.waitFor({ state: "visible", timeout: 15_000 });

        // 2. Type the server URL and Connect.
        await urlField.fill(server.serverUrl);
        await window.locator("#connect").click();

        // 3. The server's SPA takes over the window — the app shell renders.
        //    Key on the home composer heading, reliably visible on the desktop
        //    layout (the sidebar brand collapses into the macOS title-bar row
        //    and reads as hidden there).
        const landed = window.getByText("What should we build?");
        await landed.waitFor({ state: "visible", timeout: 20_000 });
        assert.ok(await landed.isVisible(), "app shell did not render after connect");
      } finally {
        // Video is flushed on close, so close and name the clip HERE — a
        // `reproduced` facet's run FAILS (the primary repro use), and the
        // before-fix footage must still be saved on that path, not just on
        // pass. Rename to the stable name the repro-agent handoff points at.
        await electronApp.close();
        saved = saveRecording(RECORD_DIR, "connect-journey");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });
  },
);
