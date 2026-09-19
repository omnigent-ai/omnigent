// Desktop shell — fullscreen must not keep the traffic-light offset on the
// sidebar header controls.
//
// On the macOS desktop layout the Search/Settings/toggle cluster is pinned in
// the title-bar strip at left:5.5rem so it clears the traffic lights (the
// [data-electron-mac] rules in web/src/index.css). macOS fullscreen removes
// the traffic lights, so the sidebar's header content must realign with the
// window's left edge instead of keeping an empty 5.5rem strip.
//
// Journey: boot into the shell on the macOS layout → controls sit beside the
// traffic lights (88px from the left edge) → enter fullscreen → the header
// content must start near the left edge; leaving fullscreen restores the
// clearance.
//
// Runs on the real Electron main process + preload against a local server.
// The macOS layout is engaged the same way tests/e2e_ui does on non-mac
// hosts: a Macintosh userAgent override (isMacElectronShell() is UA-keyed),
// so the journey is drivable on Linux CI where window fullscreen still fires
// Electron's enter-full-screen/leave-full-screen.
//
// Run: `node --test e2e/desktop_fullscreen_sidebar_controls.e2e.js`
// from web/electron, AFTER building the SPA (pnpm --filter web run build).
// Headless boxes need `xvfb-run -a` and OMNIGENT_PW_NO_SANDBOX=1.

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
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-fullscreen-sidebar-controls");

// The 5.5rem clearance the cluster keeps for the traffic lights while
// windowed; anything at or past it in fullscreen is the reported dead gap.
const TRAFFIC_LIGHT_CLEARANCE_PX = 88;
// "Aligned with the sidebar content": the leftmost header control (or the
// restored brand) must start well inside the old clearance.
const FULLSCREEN_ALIGNED_MAX_X = 48;

/** The visible main window (firstWindow() can race to a hidden helper). */
async function mainWindow(electronApp, firstWindow) {
  for (let i = 0; i < 120; i++) {
    const page = electronApp.windows().find((p) => p.url().startsWith("http"));
    if (page) return page;
    // Poll: window creation order is not deterministic at boot.
    // oxlint-disable-next-line no-await-in-loop
    await new Promise((resolve) => {
      setTimeout(resolve, 500);
    });
  }
  return firstWindow;
}

/**
 * Left edge (x) of the leftmost visible sidebar-header element: the title-bar
 * cluster, the sidebar brand, or the in-sidebar actions cluster — whichever a
 * fix keeps or restores. Null when none is visible.
 */
async function leftmostHeaderControlX(window) {
  const candidates = [
    window.locator(".electron-sidebar-header-actions"),
    window.locator('[data-testid="sidebar-brand"]'),
    window.locator('.conversations-sidebar [data-testid="sidebar-header-actions"]'),
  ];
  let min = null;
  for (const locator of candidates) {
    // Sequential probing keeps the failure message attributable per locator.
    // oxlint-disable no-await-in-loop
    if (!(await locator.isVisible().catch(() => false))) continue;
    const box = await locator.boundingBox();
    // oxlint-enable no-await-in-loop
    if (box && (min === null || box.x < min)) min = box.x;
  }
  return min;
}

/** Poll until `predicate(await probe())` holds or `timeoutMs` passes. */
async function waitForValue(probe, predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let value = await probe();
  // Polling is inherently sequential.
  /* oxlint-disable no-await-in-loop */
  while (!predicate(value) && Date.now() < deadline) {
    await new Promise((resolve) => {
      setTimeout(resolve, 250);
    });
    value = await probe();
  }
  /* oxlint-enable no-await-in-loop */
  return value;
}

describe(
  "desktop shell — fullscreen sidebar header controls",
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

    it("realigns the sidebar header controls when the window goes fullscreen", async () => {
      // Pre-seed the server: the bug is past connect, boot straight into the shell.
      const {
        electronApp,
        window: firstWindow,
        userDataDir,
        stopDisplayCapture,
      } = await launchDesktop({ recordDir: RECORD_DIR, serverUrl: server.serverUrl });
      let saved;
      try {
        const window = await mainWindow(electronApp, firstWindow);
        const landed = window.getByText("What should we build?");
        await landed.waitFor({ state: "visible", timeout: 60_000 });

        // Engage the macOS desktop layout (UA-keyed, see isMacElectronShell).
        await window.addInitScript(() => {
          Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
          Object.defineProperty(navigator, "platform", { value: "MacIntel" });
        });
        await window.reload();
        await landed.waitFor({ state: "visible", timeout: 60_000 });

        // Windowed: the cluster clears the traffic lights.
        const cluster = window.locator(".electron-sidebar-header-actions");
        await cluster.waitFor({ state: "visible", timeout: 15_000 });
        const windowedBox = await cluster.boundingBox();
        assert.ok(windowedBox, "title-bar cluster has no bounding box");
        assert.ok(
          windowedBox.x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8,
          `windowed cluster should clear the traffic lights, got x=${windowedBox.x}`,
        );

        // The user's action: enter fullscreen (green light / View > Toggle Full Screen).
        const entered = await electronApp.evaluate(
          ({ BrowserWindow }) =>
            new Promise((resolve) => {
              const win = BrowserWindow.getAllWindows().find(
                (w) => w.isVisible() && w.webContents.getURL().startsWith("http"),
              );
              const timer = setTimeout(
                () => resolve({ event: false, isFullScreen: win.isFullScreen() }),
                10_000,
              );
              win.once("enter-full-screen", () => {
                clearTimeout(timer);
                resolve({ event: true, isFullScreen: win.isFullScreen() });
              });
              win.setFullScreen(true);
            }),
        );
        assert.ok(
          entered.event && entered.isFullScreen,
          `window did not enter fullscreen: ${JSON.stringify(entered)}`,
        );

        // Fullscreen: no traffic lights, so the header content must start
        // near the window's left edge instead of leaving the 5.5rem gap.
        const fullscreenX = await waitForValue(
          () => leftmostHeaderControlX(window),
          (x) => x !== null && x < FULLSCREEN_ALIGNED_MAX_X,
          10_000,
        );
        assert.ok(fullscreenX !== null, "no sidebar header control is visible in fullscreen");
        assert.ok(
          fullscreenX < FULLSCREEN_ALIGNED_MAX_X,
          `sidebar header controls still reserve the traffic-light strip in fullscreen: ` +
            `leftmost visible control at x=${fullscreenX} (expected < ${FULLSCREEN_ALIGNED_MAX_X})`,
        );

        // Leaving fullscreen restores the clearance (the lights are back).
        await electronApp.evaluate(
          ({ BrowserWindow }) =>
            new Promise((resolve) => {
              const win = BrowserWindow.getAllWindows().find(
                (w) => w.isVisible() && w.webContents.getURL().startsWith("http"),
              );
              const timer = setTimeout(resolve, 10_000);
              win.once("leave-full-screen", () => {
                clearTimeout(timer);
                resolve();
              });
              win.setFullScreen(false);
            }),
        );
        const restoredBox = await waitForValue(
          () => cluster.boundingBox(),
          (box) => box !== null && box.x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8,
          10_000,
        );
        assert.ok(restoredBox, "title-bar cluster disappeared after leaving fullscreen");
        assert.ok(
          restoredBox.x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8,
          `windowed traffic-light clearance not restored, got x=${restoredBox.x}`,
        );
      } finally {
        // Close first — video flushes on close — so a FAILING run (the repro
        // use of this test) still yields the before-fix footage.
        await electronApp.close();
        await stopDisplayCapture();
        saved = saveRecording(RECORD_DIR, "fullscreen-sidebar-controls");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });
  },
);
