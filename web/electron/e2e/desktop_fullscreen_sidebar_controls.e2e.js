// The macOS title-bar cluster (sidebar toggle / Search / Settings) sits 5.5rem
// in to clear the traffic lights; native fullscreen hides those lights, so the
// cluster must drop that clearance while fullscreen and restore it afterwards.

// Run from web/electron after building the SPA (a Macintosh UA engages the layout):
//   OMNIGENT_PYTHON=../../.venv/bin/python OMNIGENT_PW_NO_SANDBOX=1 \
//     xvfb-run -a node --test e2e/desktop_fullscreen_sidebar_controls.e2e.js

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
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-fullscreen-sidebar-controls");

const CLUSTER = ".electron-sidebar-header-actions";
// Windowed clearance is `left: 5.5rem`; anything inside the old gap counts as
// realigned, so the threshold is deliberately loose. These mirror the cluster
// offsets in web/src/index.css (the Chromium stand-in test uses the same two).
const TRAFFIC_LIGHT_CLEARANCE_PX = 88;
const FULLSCREEN_ALIGNED_MAX_X = 48;
const SETTLE_MS = 10_000;
const LINGER_MS = 1_500;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function clusterX(window) {
  const box = await window.locator(CLUSTER).boundingBox();
  return box ? box.x : null;
}

async function waitForClusterX(window, predicate) {
  const deadline = Date.now() + SETTLE_MS;
  let x = await clusterX(window);
  /* oxlint-disable no-await-in-loop */
  while (!predicate(x) && Date.now() < deadline) {
    await sleep(250);
    x = await clusterX(window);
  }
  /* oxlint-enable no-await-in-loop */
  return x;
}

// isMacElectronShell() keys the macOS layout off navigator.userAgent containing
// "Macintosh"; define it in the renderer so the stand-in engages that layout
// without a real macOS host.
async function useMacintoshUserAgent(window) {
  await window.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      configurable: true,
    });
    Object.defineProperty(navigator, "platform", { value: "MacIntel", configurable: true });
  });
  await window.reload();
}

// Same window operation as the green traffic light / View > Toggle Full Screen.
function setFullScreen(electronApp, fullScreen) {
  return electronApp.evaluate(
    ({ BrowserWindow }, wanted) =>
      new Promise((resolve) => {
        const win = BrowserWindow.getAllWindows().find((w) =>
          w.webContents.getURL().startsWith("http"),
        );
        if (!win) {
          resolve({ event: false, isFullScreen: null, error: "no http window found" });
          return;
        }
        if (win.isFullScreen() === wanted) {
          resolve({ event: true, isFullScreen: wanted });
          return;
        }
        const event = wanted ? "enter-full-screen" : "leave-full-screen";
        const timer = setTimeout(
          () => resolve({ event: false, isFullScreen: win.isFullScreen() }),
          10_000,
        );
        win.once(event, () => {
          clearTimeout(timer);
          resolve({ event: true, isFullScreen: win.isFullScreen() });
        });
        win.setFullScreen(wanted);
      }),
    fullScreen,
  );
}

function layoutFacts(window) {
  return window.evaluate(() => {
    const shell = document.querySelector(".app-shell");
    return {
      userAgent: navigator.userAgent,
      appShellData: shell ? { ...shell.dataset } : null,
      displayModeFullscreen: window.matchMedia("(display-mode: fullscreen)").matches,
      fullscreenElement: Boolean(document.fullscreenElement),
      innerSize: [window.innerWidth, window.innerHeight],
    };
  });
}

describe(
  "desktop shell — sidebar header controls across native fullscreen",
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

    it("drops the traffic-light clearance while the window is fullscreen", async () => {
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir: RECORD_DIR,
        serverUrl: server.serverUrl,
      });
      const observations = {};
      let saved;
      try {
        const landed = window.getByText("What should we build?");
        await landed.waitFor({ state: "visible", timeout: 60_000 });

        await useMacintoshUserAgent(window);
        await landed.waitFor({ state: "visible", timeout: 60_000 });
        const facts = await layoutFacts(window);
        assert.equal(
          facts.appShellData?.electronMac,
          "true",
          `macOS desktop layout did not engage: ${JSON.stringify(facts)}`,
        );
        await window.locator(CLUSTER).waitFor({ state: "visible", timeout: 15_000 });

        const windowedX = await clusterX(window);
        observations.windowed = { clusterX: windowedX, ...facts };
        await window.screenshot({ path: path.join(RECORD_DIR, "windowed.png") });
        assert.ok(
          windowedX !== null && windowedX >= TRAFFIC_LIGHT_CLEARANCE_PX - 8,
          `windowed cluster should clear the traffic lights, got x=${windowedX}`,
        );
        await sleep(LINGER_MS);

        const entered = await setFullScreen(electronApp, true);
        assert.ok(
          entered.event && entered.isFullScreen,
          `window did not enter fullscreen: ${JSON.stringify(entered)}`,
        );
        const fullscreenX = await waitForClusterX(
          window,
          (x) => x !== null && x < FULLSCREEN_ALIGNED_MAX_X,
        );
        observations.fullscreen = { clusterX: fullscreenX, ...(await layoutFacts(window)) };
        await window.screenshot({ path: path.join(RECORD_DIR, "fullscreen.png") });
        await sleep(LINGER_MS);
        assert.ok(fullscreenX !== null, "sidebar header cluster is not visible in fullscreen");
        assert.ok(
          fullscreenX < FULLSCREEN_ALIGNED_MAX_X,
          "sidebar header controls still reserve the traffic-light strip in fullscreen: " +
            `cluster at x=${fullscreenX} (expected < ${FULLSCREEN_ALIGNED_MAX_X})`,
        );

        const left = await setFullScreen(electronApp, false);
        assert.ok(
          left.event && !left.isFullScreen,
          `window did not leave fullscreen: ${JSON.stringify(left)}`,
        );
        const restoredX = await waitForClusterX(
          window,
          (x) => x !== null && x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8,
        );
        observations.restored = { clusterX: restoredX, ...(await layoutFacts(window)) };
        await window.screenshot({ path: path.join(RECORD_DIR, "restored.png") });
        assert.ok(
          restoredX !== null && restoredX >= TRAFFIC_LIGHT_CLEARANCE_PX - 8,
          `windowed traffic-light clearance not restored after leaving fullscreen, got x=${restoredX}`,
        );
        await sleep(LINGER_MS);
      } finally {
        fs.writeFileSync(
          path.join(RECORD_DIR, "observations.json"),
          JSON.stringify(observations, null, 2),
        );
        // Close first so a failing run still flushes and names its footage.
        // Guard each cleanup step so one failing step neither masks the test's
        // own error nor skips removing the temp profile below.
        await electronApp.close().catch(() => {});
        await stopDisplayCapture().catch(() => {});
        try {
          saved = saveRecording(RECORD_DIR, "fullscreen-sidebar-controls");
        } finally {
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });
  },
);
