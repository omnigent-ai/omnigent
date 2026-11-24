// "Open in new window" pops a session out; opening it again brings that window forward.
// Run: OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_session_window.e2e.js
// Env: OMNIGENT_PYTHON (backend), OMNIGENT_DESKTOP_RECORD_DIR (paced recording).

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually, jsonRequest, startDesignBackend } = require("./desktopDesignPromptHarness");

const RECORD_DIR = process.env.OMNIGENT_DESKTOP_RECORD_DIR;
const PRIOR_REPLY = "The rollout plan has two phases.";

const deps = desktopDepsAvailable();

// Step times relative to the recording start, for caption cues.
let recordingStart = Date.now();
function step(label) {
  if (RECORD_DIR)
    console.log(`step ${((Date.now() - recordingStart) / 1000).toFixed(1)}s ${label}`);
}

function pause(ms = 1_500) {
  return new Promise((resolve) => {
    setTimeout(resolve, RECORD_DIR ? ms : 0);
  });
}

function jsonBody(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

/** Each app window showing the server (not the shell's own overlays), read in the main process. */
function listWindows(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows()
      .filter((win) => !win.isDestroyed() && /^https?:/.test(win.webContents.getURL()))
      .map((win) => ({
        id: win.id,
        url: win.webContents.getURL(),
        size: win.getSize(),
        focused: win.isFocused(),
      })),
  );
}

async function openFromHeaderMenu(window) {
  await window.getByTestId("header-conversation-actions").click();
  await pause(1_200);
  await window.getByTestId("header-open-session-window").click();
}

it(
  "opens a session in its own window and brings that window back on a second open",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 240_000 },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-session-window-"));
    const recordDir = RECORD_DIR || path.join(tmpDir, "recordings");
    let backend;
    let app;
    try {
      backend = await startDesignBackend(tmpDir);
      const { serverUrl, mockUrl, sessionId } = backend;
      const session = `${serverUrl}/v1/sessions/${sessionId}`;
      await jsonRequest(session, jsonBody("PATCH", { title: "Rollout plan" }));
      await jsonRequest(
        `${mockUrl}/mock/set_fallback`,
        jsonBody("POST", { key: "gpt-4o-mini", text: PRIOR_REPLY, stream: true }),
      );
      await jsonRequest(
        `${session}/events`,
        jsonBody("POST", {
          type: "message",
          data: { role: "user", content: [{ type: "input_text", text: "Outline the rollout." }] },
        }),
      );
      await eventually(
        async () => JSON.stringify(await jsonRequest(`${session}/items`)).includes(PRIOR_REPLY),
        "prior reply",
        60_000,
      );

      recordingStart = Date.now();
      app = await launchDesktop({
        serverUrl,
        recordDir,
        userDataDir: path.join(tmpDir, "profile"),
      });
      const { window, electronApp } = app;
      await window.goto(`${serverUrl}/c/${sessionId}`);
      await window.getByText(PRIOR_REPLY).first().waitFor({ timeout: 45_000 });
      step("session open in the main window");
      await pause();
      const before = await listWindows(electronApp);
      assert.equal(before.length, 1);

      await openFromHeaderMenu(window);
      step("Open in new window clicked");
      const popOut = await eventually(
        () => electronApp.windows().find((page) => page.url().includes("?window=session")),
        "pop-out window",
        20_000,
      );
      await popOut.getByText(PRIOR_REPLY).first().waitFor({ timeout: 45_000 });
      step("session shown in the new window");
      assert.equal(
        new URL(popOut.url()).pathname + new URL(popOut.url()).search,
        `/c/${sessionId}?window=session`,
      );
      assert.equal(
        await popOut.evaluate(() => document.documentElement.dataset.sessionWindow),
        "true",
      );
      await popOut.getByRole("button", { name: "Open sidebar" }).waitFor();
      const afterOpen = await listWindows(electronApp);
      assert.equal(afterOpen.length, 2, JSON.stringify(afterOpen));
      const popOutWindow = afterOpen.find((win) => win.id !== before[0].id);
      assert.deepEqual(popOutWindow.size, [960, 820]);
      assert.equal(new URL(window.url()).pathname, `/c/${sessionId}`);
      await pause(2_500);

      // Opening the same session again focuses the existing pop-out without reloading it.
      await popOut.evaluate(() => {
        window.popOutStillLoaded = true;
      });
      await electronApp.evaluate(({ BrowserWindow }, id) => {
        BrowserWindow.fromId(id).focus();
      }, before[0].id);
      await eventually(
        async () =>
          !(await listWindows(electronApp)).find((win) => win.id === popOutWindow.id).focused,
        "main window focused",
        10_000,
      );
      await pause(800);
      await openFromHeaderMenu(window);
      step("Open in new window clicked again");
      await eventually(
        async () =>
          (await listWindows(electronApp)).find((win) => win.id === popOutWindow.id)?.focused,
        "existing pop-out focused",
        10_000,
      );
      const afterReopen = await listWindows(electronApp);
      assert.deepEqual(
        afterReopen.map((win) => win.id).sort(),
        afterOpen.map((win) => win.id).sort(),
      );
      assert.equal(await popOut.evaluate(() => window.popOutStillLoaded), true);
      step("existing window brought forward, no duplicate");
      await pause(3_000);
      // End the clip on this state rather than on the windows closing.
      await app.stopDisplayCapture();
    } finally {
      if (app) {
        await app.electronApp.close().catch(() => {});
        await app.stopDisplayCapture();
        const saved = saveRecording(recordDir, "session-window");
        if (RECORD_DIR) console.log(`Desktop session-window recordings: ${saved.join(", ")}`);
      }
      await backend?.close();
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
