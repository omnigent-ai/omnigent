"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { setTimeout: sleep } = require("node:timers/promises");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");

const deps = desktopDepsAvailable();
// A healthy saved server loads in seconds; this leaves room for a bounded load timeout.
const LEAVE_LOADING_WITHIN_MS = 90_000;

async function shellState(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }) => {
    const windows = BrowserWindow.getAllWindows();
    const shell = windows.find((win) => win.getParentWindow() === null);
    return {
      shellUrl: shell?.webContents.getURL() ?? null,
      loadingIndicatorVisible: windows.some(
        (win) =>
          win.getParentWindow() === shell &&
          win.isVisible() &&
          /connection-loading\/index\.html$/.test(win.webContents.getURL()),
      ),
    };
  });
}

async function pageAt(electronApp, url) {
  const deadline = Date.now() + 10_000;
  for (;;) {
    const page = electronApp.windows().find((candidate) => candidate.url() === url);
    if (page || Date.now() > deadline) return page;
    // oxlint-disable-next-line no-await-in-loop -- Playwright sees the navigation after the main process
    await sleep(100);
  }
}

it(
  "leaves the loading screen when the saved server accepts connections but never answers",
  {
    skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`,
    timeout: LEAVE_LOADING_WITHIN_MS + 60_000,
  },
  async () => {
    const recordDir =
      process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
      fs.mkdtempSync(path.join(os.tmpdir(), "omni-unresponsive-recordings-"));
    // Stands in for a hung local server: the port accepts TCP connections but sends nothing.
    const sockets = new Set();
    const hungServer = net.createServer((socket) => sockets.add(socket));
    await new Promise((resolve) => {
      hungServer.listen(0, "127.0.0.1", resolve);
    });
    const serverUrl = `http://127.0.0.1:${hungServer.address().port}`;

    let state;
    try {
      const { electronApp, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir,
        serverUrl,
        waitForShell: false,
      });
      try {
        const deadline = Date.now() + LEAVE_LOADING_WITHIN_MS;
        for (;;) {
          // oxlint-disable-next-line no-await-in-loop -- poll the shell until it settles
          state = await shellState(electronApp);
          if (state.shellUrl && !state.loadingIndicatorVisible) break;
          if (Date.now() > deadline) break;
          // oxlint-disable-next-line no-await-in-loop -- poll interval
          await sleep(500);
        }
        if (state.shellUrl && !state.loadingIndicatorVisible) {
          const shell = await pageAt(electronApp, state.shellUrl);
          assert.ok(shell, `no Playwright page for ${state.shellUrl}`);
          await shell
            .getByText("ERR_TIMED_OUT (-7)")
            .waitFor({ state: "visible", timeout: 10_000 });
          // Keep the outcome on screen for the recording.
          await sleep(2_000);
        }
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        saveRecording(recordDir, "saved-server-unresponsive");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    } finally {
      for (const socket of sockets) socket.destroy();
      hungServer.close();
    }
    assert.ok(
      state.shellUrl && !state.loadingIndicatorVisible,
      `still on "Opening Omnigent…" ${LEAVE_LOADING_WITHIN_MS / 1000}s after launch: ${JSON.stringify(state)}`,
    );
    const setup = new URL(state.shellUrl);
    assert.equal(setup.protocol, "file:");
    assert.equal(setup.searchParams.get("error"), "ERR_TIMED_OUT (-7)");
    assert.equal(setup.searchParams.get("url"), `${serverUrl}/`);
  },
);
