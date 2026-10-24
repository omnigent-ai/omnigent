// Desktop e2e: downloading a workspace file from the Files panel when the
// deployment routes host-scoped requests by the app's slice-key header (local
// server + real host behind the routing-ingress stand-in from fixtures/).

// Run from web/electron after building the SPA:
//   OMNIGENT_PYTHON=../../.venv/bin/python OMNIGENT_PW_NO_SANDBOX=1 \
//     xvfb-run -a node --test e2e/desktop_workspace_download.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  APP_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");
const { spawnHost, createHostSession } = require("./fixtures/hostStack");
const { startRoutingIngress } = require("./fixtures/routingIngress");

const deps = desktopDepsAvailable();
// Recordings, screenshots and the ingress log; an explicit dir also paces the journey.
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-workspace-download");
const FILE_NAME = "report.txt";
const FILE_BYTES = Buffer.from(
  Array.from({ length: 64 }, (_, i) => `line ${i}: workspace download probe\n`).join(""),
);
const DOWNLOAD_WAIT_MS = 30_000;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

// Pauses that keep a recorded journey legible; skipped unless a record dir is chosen.
const PACED = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);
const pace = (ms) => (PACED ? sleep(ms) : Promise.resolve());

/** A standalone SPA bundle with host routing on: OMNIGENT_ROUTING_WEB_UI_DIST, else built once. */
function ensureRoutingDist(tmpDir) {
  const given = process.env.OMNIGENT_ROUTING_WEB_UI_DIST;
  if (given && fs.existsSync(path.join(given, "index.html"))) return given;
  const out = path.join(tmpDir, "web-ui-routing");
  const web = path.resolve(APP_ROOT, "..");
  const result = spawnSync(
    path.join(web, "node_modules", ".bin", "vite"),
    ["build", "--outDir", out, "--emptyOutDir"],
    {
      cwd: web,
      env: { ...process.env, VITE_OMNIGENT_HOST_ROUTING: "true" },
      encoding: "utf8",
      timeout: 300_000,
      maxBuffer: 16 * 1024 * 1024,
    },
  );
  if (result.error) throw new Error(`routing SPA build failed to start: ${result.error.message}`);
  if (result.status !== 0) {
    throw new Error(
      `routing SPA build failed:\n${(result.stderr || result.stdout || "").slice(-3000)}`,
    );
  }
  return out;
}

/** Record every download the shell starts, saving each to its own file under `saveDir`. */
async function watchDownloads(electronApp, saveDir) {
  fs.mkdirSync(saveDir, { recursive: true });
  await electronApp.evaluate(({ session }, dir) => {
    globalThis.omniDownloadRecords = [];
    session.defaultSession.on("will-download", (_event, item) => {
      const record = {
        url: item.getURL(),
        filename: item.getFilename(),
        startState: item.getState(),
        done: null,
        receivedBytes: 0,
        savePath: `${dir}/${globalThis.omniDownloadRecords.length}-${item.getFilename().replaceAll("/", "_")}`,
      };
      globalThis.omniDownloadRecords.push(record);
      // A preset path skips the native Save dialog.
      item.setSavePath(record.savePath);
      item.on("done", (_e, state) => {
        record.done = state;
        record.receivedBytes = item.getReceivedBytes();
      });
    });
  }, saveDir);
}

const readDownloads = (electronApp) =>
  electronApp.evaluate(() => globalThis.omniDownloadRecords ?? []);

/** Resolve once the first `count` downloads have finished; fail if they do not in time. */
async function waitForDownloadDone(electronApp, count) {
  const deadline = Date.now() + DOWNLOAD_WAIT_MS;
  // oxlint-disable no-await-in-loop -- polling the main process.
  for (;;) {
    const downloads = await readDownloads(electronApp);
    const settled = downloads.slice(0, count);
    const bad = settled.find((d) => d.done && d.done !== "completed");
    if (bad) throw new Error(`download ended in state ${bad.done}: ${JSON.stringify(bad)}`);
    if (downloads.length >= count && settled.every((d) => d.done)) {
      return downloads;
    }
    if (Date.now() >= deadline) {
      throw new Error(
        `${count} download(s) did not finish within ${DOWNLOAD_WAIT_MS} ms: ${JSON.stringify(downloads)}`,
      );
    }
    await sleep(250);
  }
  // oxlint-enable no-await-in-loop
}

/** Navigate the shell to the session and open the Files tab; returns the row. */
async function openFilesPanel(window, baseUrl, sessionId) {
  await window.goto(`${baseUrl}/c/${sessionId}`);
  const toggle = window
    .locator('button[aria-label="Expand right panel"], button[aria-label="Collapse right panel"]')
    .first();
  await toggle.waitFor({ state: "visible", timeout: 60_000 });
  if ((await toggle.getAttribute("aria-label")) === "Expand right panel") await toggle.click();
  const rail = window.getByRole("complementary", { name: "Workspace" });
  await rail.waitFor({ state: "visible", timeout: 15_000 });
  await rail.getByRole("tab", { name: /^Files/ }).click();
  const row = rail.getByRole("button", { name: FILE_NAME }).filter({ hasText: FILE_NAME });
  await row.waitFor({ state: "visible", timeout: 60_000 });
  return { rail, row };
}

/** Tunnel-dependent requests the ingress has seen so far, by how they were keyed. */
function tunnelRequestSummary(ingress, hostId) {
  const tunnel = ingress.requests.filter((r) => r.tunnelRoute);
  return {
    tunnelRequests: tunnel.length,
    keyedWithHost: tunnel.filter((r) => r.sliceKey === hostId).length,
    wrongReplica: tunnel.filter((r) => r.decision === "wrong-replica").map((r) => r.path),
  };
}

/** Whether the viewer's "Download failed" toast is showing (the row icon only has a hover tooltip). */
async function failureTextVisible(window) {
  const text = window.getByText("Download failed");
  return (await text.count()) > 0 && (await text.first().isVisible());
}

function assertSavedFile(download) {
  assert.equal(
    download.done,
    "completed",
    `download did not complete: ${JSON.stringify(download)}`,
  );
  assert.ok(
    fs.existsSync(download.savePath) && fs.readFileSync(download.savePath).equals(FILE_BYTES),
    `saved file differs from the workspace file: ${download.savePath}`,
  );
}

describe(
  "desktop shell — workspace file download on a slice-key-routed deployment",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let host;
    let ingress;
    let session;
    let workspace;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-download-"));
      fs.mkdirSync(RECORD_DIR, { recursive: true });
      const routingDist = ensureRoutingDist(tmpDir);
      server = await spawnServer(tmpDir, { env: () => ({ OMNIGENT_WEB_UI_DIST: routingDist }) });
      host = await spawnHost(server.serverUrl, tmpDir, {
        env: { OPENAI_BASE_URL: `${server.mockUrl}/v1`, OPENAI_API_KEY: "mock-key" },
      });
      workspace = path.join(tmpDir, "workspace");
      fs.mkdirSync(workspace, { recursive: true });
      fs.writeFileSync(path.join(workspace, FILE_NAME), FILE_BYTES);
      session = await createHostSession(server.serverUrl, { hostId: host.hostId, workspace });
      ingress = await startRoutingIngress(server.serverUrl, {
        logPath: path.join(RECORD_DIR, "ingress-requests.jsonl"),
      });
      fs.writeFileSync(
        path.join(RECORD_DIR, "stack.json"),
        JSON.stringify(
          {
            serverUrl: server.serverUrl,
            ingressUrl: ingress.url,
            hostId: host.hostId,
            sessionId: session.sessionId,
            runnerId: session.runnerId,
            workspace,
            routingDist,
          },
          null,
          2,
        ),
      );
    });

    after(async () => {
      // Close everything even when one step throws, so no process or tmp dir leaks.
      const results = await Promise.allSettled([ingress?.close(), host?.close(), server?.close()]);
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
      const failures = results.filter((r) => r.status === "rejected").map((r) => r.reason);
      if (failures.length === 1) throw failures[0];
      if (failures.length > 1) throw new AggregateError(failures, "teardown steps failed");
    });

    it("downloading through the ingress carries the slice key and saves the complete file", async () => {
      const recordDir = path.join(RECORD_DIR, "routed");
      // Wall-clock marks relative to the display capture start, for captioning.
      const captureStart = Date.now();
      const marks = {};
      const mark = (name) => {
        marks[name] = (Date.now() - captureStart) / 1000;
      };
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir,
        serverUrl: ingress.url,
      });
      const observed = {
        listing: null,
        viewerMenu: {},
        downloads: [],
        downloadRequests: [],
        marks,
      };
      try {
        await watchDownloads(electronApp, path.join(tmpDir, "saved-routed"));
        await window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        mark("shellConnected");
        await pace(1500);
        const { rail, row } = await openFilesPanel(window, ingress.url, session.sessionId);
        mark("filesPanelListed");
        observed.listing = tunnelRequestSummary(ingress, host.hostId);
        await pace(2500);

        // 1. File-row download icon (hover-revealed).
        await row.hover();
        await pace(1200);
        await rail.getByRole("button", { name: `Download ${FILE_NAME}` }).click();
        mark("rowIconClicked");
        await waitForDownloadDone(electronApp, 1);
        mark("rowIconObserved");
        await window.screenshot({ path: path.join(recordDir, "after-row-icon-click.png") });
        await pace(2000);

        // 2. File viewer > View settings > Download file.
        await row.click();
        await rail.getByTestId("file-viewer").waitFor({ state: "visible", timeout: 15_000 });
        mark("viewerOpened");
        await pace(1500);
        await rail.getByRole("button", { name: "View settings" }).click();
        mark("viewSettingsOpened");
        await pace(1200);
        await window.getByRole("menuitem", { name: "Download file" }).click();
        mark("viewerDownloadClicked");
        await waitForDownloadDone(electronApp, 2);
        // Let the renderer flush a failure toast before the negative check.
        await sleep(500);
        observed.viewerMenu = { failureTextVisible: await failureTextVisible(window) };
        mark("viewerDownloadObserved");
        await window.screenshot({ path: path.join(recordDir, "after-viewer-download.png") });
        await pace(3000);
        mark("end");
      } finally {
        observed.downloads = await readDownloads(electronApp).catch(() => observed.downloads);
        observed.downloadRequests = ingress.requests.filter((r) =>
          r.path.includes("download=true"),
        );
        fs.writeFileSync(path.join(recordDir, "observed.json"), JSON.stringify(observed, null, 2));
        // Stop filming before the window closes so the clip ends on the app state;
        // never let teardown mask the test's own failure or skip the cleanup below.
        await stopDisplayCapture().catch(() => {});
        await electronApp.close().catch(() => {});
        try {
          saveRecording(recordDir, "routed-download");
        } catch {
          // Renaming the clips must not skip the profile cleanup below.
        }
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      // The app's own listing requests routed by key, so a keyless download is a bypass.
      assert.ok(
        observed.listing.keyedWithHost > 0,
        `no keyed listing requests: ${JSON.stringify(observed.listing)}`,
      );
      assert.deepEqual(
        observed.listing.wrongReplica,
        [],
        "listing requests reached a wrong replica",
      );
      assert.equal(
        observed.downloadRequests.length,
        2,
        `expected one download request per action: ${JSON.stringify(observed.downloadRequests)}`,
      );
      for (const request of observed.downloadRequests) {
        assert.equal(
          request.sliceKey,
          host.hostId,
          `download request bypassed the app transport (no slice key): ${JSON.stringify(request)}`,
        );
      }
      assert.equal(
        observed.downloads.length,
        2,
        `expected one download per action: ${JSON.stringify(observed.downloads)}`,
      );
      for (const download of observed.downloads) assertSavedFile(download);
      assert.equal(
        observed.viewerMenu.failureTextVisible,
        false,
        "the viewer showed Download failed",
      );
    });
  },
);
