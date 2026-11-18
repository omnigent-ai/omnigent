// Clicking an `omnigent://<host>/c/<id>` link to this server in a reply opens that chat in the same window.
// Run: OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_session_deep_link.e2e.js
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
const WORKER_REPLY = "Build finished: all 42 tests passed.";
const LINK_TEXT = "the build worker chat";

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

function setMockReply(mockUrl, text) {
  return jsonRequest(
    `${mockUrl}/mock/set_fallback`,
    jsonBody("POST", { key: "gpt-4o-mini", text, stream: true }),
  );
}

it(
  "opens the chat an omnigent:// link in a reply names, in the same window",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 240_000 },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-session-deep-link-"));
    const recordDir = RECORD_DIR || path.join(tmpDir, "recordings");
    let backend;
    let app;
    try {
      backend = await startDesignBackend(tmpDir);
      const { serverUrl, mockUrl, sessionId, otherSessionId: workerId } = backend;
      const session = (id) => `${serverUrl}/v1/sessions/${id}`;
      await jsonRequest(session(workerId), jsonBody("PATCH", { title: "Build worker" }));
      await jsonRequest(session(sessionId), jsonBody("PATCH", { title: "Coordinator" }));

      // The worker chat already has a finished turn to land on.
      await setMockReply(mockUrl, WORKER_REPLY);
      await jsonRequest(
        `${session(workerId)}/events`,
        jsonBody("POST", {
          type: "message",
          data: { role: "user", content: [{ type: "input_text", text: "Run the build." }] },
        }),
      );
      await eventually(
        async () =>
          JSON.stringify(await jsonRequest(`${session(workerId)}/items`)).includes(WORKER_REPLY),
        "worker reply",
        60_000,
      );
      const deepLink = `omnigent://${new URL(serverUrl).host}/c/${workerId}`;
      await setMockReply(
        mockUrl,
        `I started a worker for the build. Follow it in [${LINK_TEXT}](${deepLink}).`,
      );

      recordingStart = Date.now();
      app = await launchDesktop({
        serverUrl,
        recordDir,
        userDataDir: path.join(tmpDir, "profile"),
      });
      const { window, electronApp } = app;
      await window.goto(`${serverUrl}/c/${sessionId}`);
      const composer = window.getByRole("textbox", { name: "Message the agent" });
      await composer.waitFor({ timeout: 45_000 });
      step("coordinator chat open");
      await pause();

      await composer.fill("Start a worker for the build and link me to it.");
      await pause(800);
      await window.getByRole("button", { name: "Send", exact: true }).click();
      step("message sent");

      const link = window.getByRole("link", { name: LINK_TEXT });
      await link.waitFor({ timeout: 60_000 });
      // The reply streams in; wait for the finished link, routed in the app rather than to the OS.
      await eventually(
        async () => (await link.getAttribute("href")) === `/c/${workerId}`,
        "session link routed in the app",
        20_000,
      );
      assert.equal(await window.getByText("[blocked]").count(), 0);
      step("reply shows the session link");
      await link.hover();
      await pause(2_500);

      const windowsBefore = await electronApp.evaluate(({ BrowserWindow }) =>
        BrowserWindow.getAllWindows().map((win) => win.id),
      );
      await link.click();
      step("link clicked");
      await window.waitForURL(`${serverUrl}/c/${workerId}`, { timeout: 20_000 });
      await window.getByText(WORKER_REPLY).first().waitFor({ timeout: 20_000 });
      step("worker chat open");
      const windowsAfter = await electronApp.evaluate(({ BrowserWindow }) =>
        BrowserWindow.getAllWindows().map((win) => win.id),
      );
      assert.deepEqual(windowsAfter, windowsBefore, "the link opened or closed a window");
      await pause(3_000);
      // End the clip on this state rather than on the window closing.
      await app.stopDisplayCapture();
    } finally {
      if (app) {
        await app.electronApp.close().catch(() => {});
        await app.stopDisplayCapture();
        const saved = saveRecording(recordDir, "session-deep-link");
        if (RECORD_DIR) console.log(`Desktop session deep-link recordings: ${saved.join(", ")}`);
      }
      await backend?.close();
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
