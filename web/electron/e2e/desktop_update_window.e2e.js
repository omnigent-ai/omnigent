// Desktop-shell reproduction: the update prompt must not be presented as a
// separate OS window alongside the app window.
//
// Journey: launch the desktop app connected to a server; a newer desktop
// version is published on the update feed; the update prompt card appears
// bottom-right. Expected: the prompt reads as part of the app window. Observed
// bug: the prompt is hosted in a second visible, FOCUSABLE top-level OS window
// (titled "Omnigent Update"), so window switchers / Mission Control / screen
// -share pickers list it as another app window and clicking the card moves OS
// focus out of the app.
//
// Run from web/electron, after building the SPA and the overlay bundle:
//   pnpm --filter web run build && pnpm --filter web run build:overlay
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_update_window.e2e.js
// Skips cleanly when electron or playwright aren't installed.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const {
  APP_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-update-window");
const OVERLAY_PAGE = path.join(APP_ROOT, "overlay", "update-overlay.html");

// A newer-than-running version on every electron-updater channel file
// (latest.yml / latest-mac.yml / latest-linux.yml). launchDesktop pins the
// running app to 999.0.0, so 9999.0.0 always reads as an available update.
const FEED_VERSION = "9999.0.0";
const FEED_YML = [
  `version: ${FEED_VERSION}`,
  "files:",
  `  - url: Omnigent-${FEED_VERSION}-x86_64.AppImage`,
  "    sha512: aGVsbG8=",
  "    size: 1024",
  `path: Omnigent-${FEED_VERSION}-x86_64.AppImage`,
  "sha512: aGVsbG8=",
  "releaseDate: '2026-01-01T00:00:00.000Z'",
  "",
].join("\n");

/** Serve FEED_YML for any *.yml request; 404 otherwise. */
function startFeed() {
  return new Promise((resolve) => {
    const feed = http.createServer((req, res) => {
      const pathname = new URL(req.url, "http://feed.invalid").pathname;
      if (pathname.endsWith(".yml")) {
        res.writeHead(200, { "content-type": "text/yaml" });
        res.end(FEED_YML);
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    feed.listen(0, "127.0.0.1", () => {
      resolve({
        url: `http://127.0.0.1:${feed.address().port}/`,
        close: () =>
          new Promise((r) => {
            feed.close(r);
          }),
      });
    });
  });
}

describe(
  "desktop shell — update prompt window",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let feed;

    before(async () => {
      assert.ok(
        fs.existsSync(OVERLAY_PAGE),
        `overlay bundle missing at ${OVERLAY_PAGE}. Build it first:\n` +
          "  pnpm --filter web run build:overlay",
      );
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir);
      feed = await startFeed();
    });

    after(async () => {
      if (feed) await feed.close();
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("shows the update prompt without opening a separate OS window alongside the app", async () => {
      const {
        electronApp,
        window: firstWindow,
        userDataDir,
      } = await launchDesktop({
        recordDir: RECORD_DIR,
        serverUrl: server.serverUrl,
      });
      try {
        // 1. The app shell renders (we're connected to the local server).
        //    firstWindow() may hand back the overlay page (its file:// load
        //    settles before the SPA), so pick the shell window explicitly.
        let window = firstWindow;
        // Polling is inherently sequential — each probe must await the backoff.
        /* oxlint-disable no-await-in-loop */
        for (let i = 0; i < 50 && window.url().includes("update-overlay"); i++) {
          const shell = electronApp.windows().find((p) => !p.url().includes("update-overlay"));
          if (shell) {
            window = shell;
            break;
          }
          await firstWindow.waitForTimeout(500);
        }
        /* oxlint-enable no-await-in-loop */
        assert.ok(!window.url().includes("update-overlay"), "shell window never appeared");
        await window.waitForLoadState("domcontentloaded");
        const landed = window.getByText("What should we build?");
        await landed.waitFor({ state: "visible", timeout: 20_000 });

        // 2. A newer version becomes available: point the app's REAL updater
        //    at the mock feed and run the same check the "Check for Updates…"
        //    menu / periodic timer performs. Resolve electron-updater by
        //    realpath so we get the SAME cached module instance main.js uses
        //    (Node caches modules by resolved filename; pnpm paths are links).
        const updaterMain = fs.realpathSync(
          require.resolve("electron-updater", { paths: [APP_ROOT] }),
        );
        let checked = "";
        // Retries are inherently sequential — each attempt must await the last.
        /* oxlint-disable no-await-in-loop */
        for (let attempt = 0; attempt < 5 && !checked.startsWith("ok"); attempt++) {
          // The startup dev-feed check can still be in flight on the first
          // attempts (rejects with ERR_ABORTED); retry until our feed answers.
          checked = await electronApp.evaluate(
            async (_electron, { url, updaterPath }) => {
              const req = typeof require === "function" ? require : process.mainModule.require;
              const { autoUpdater } = req(updaterPath);
              autoUpdater.setFeedURL({ provider: "generic", url });
              try {
                const res = await autoUpdater.checkForUpdates();
                return "ok " + (res && res.updateInfo && res.updateInfo.version);
              } catch (err) {
                return "err " + (err && err.message);
              }
            },
            { url: feed.url, updaterPath: updaterMain },
          );
          if (!checked.startsWith("ok")) await window.waitForTimeout(2_000);
        }
        /* oxlint-enable no-await-in-loop */
        assert.equal(checked, `ok ${FEED_VERSION}`, `update check never succeeded: ${checked}`);

        // 3. The update prompt card renders ("Omnigent Desktop … is available").
        const overlayPage = electronApp
          .windows()
          .find((page) => page.url().includes("update-overlay"));
        assert.ok(overlayPage, "update overlay page not found");
        await overlayPage.getByText("is available").waitFor({ state: "visible", timeout: 15_000 });
        // Let the card settle on screen so the failing run's video shows it.
        await window.waitForTimeout(1_500);

        // 4. THE BUG: the prompt is hosted in a separate visible, focusable
        //    top-level OS window ("Omnigent Update") alongside the app window,
        //    so the OS presents it as a second window (window switchers,
        //    Mission Control, screen-share pickers) and clicking the card
        //    moves OS focus out of the app. A fixed build either hosts the
        //    card inside the app window (no such window) or keeps the overlay
        //    window non-focusable so it is never presented alongside the app.
        const separateWindows = await electronApp.evaluate(({ BrowserWindow }) =>
          BrowserWindow.getAllWindows()
            .filter((w) => w.webContents.getURL().includes("update-overlay"))
            .filter((w) => w.isVisible() && w.isFocusable())
            .map((w) => ({ title: w.getTitle(), bounds: w.getBounds() })),
        );
        assert.equal(
          separateWindows.length,
          0,
          "update prompt is presented as a separate focusable OS window " +
            `alongside the app: ${JSON.stringify(separateWindows)}`,
        );
      } finally {
        // Close FIRST (flushes the videos), then name the clips — the failing
        // path (the reproduction) must still produce the before-fix footage.
        await electronApp.close();
        saveRecording(RECORD_DIR, "update-window");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    });
  },
);
