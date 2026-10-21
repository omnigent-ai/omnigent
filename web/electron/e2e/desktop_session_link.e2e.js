// Run after building web: node --test e2e/desktop_session_link.e2e.js
// OMNIGENT_PYTHON selects the backend; use an absolute path since the backend runs with cwd=tmpDir.

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { startDesignBackend } = require("./desktopDesignPromptHarness");

const deps = desktopDepsAvailable();
const FAKE_BROWSER = path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs");

it(
  "opens a chat link to a session on the connected server inside the desktop window",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 240_000 },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-session-link-"));
    const recordDir =
      process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
      fs.mkdtempSync(path.join(os.tmpdir(), "omni-session-link-recordings-"));
    let backend;
    let desktop;
    try {
      backend = await startDesignBackend(tmpDir);
      const { serverUrl, sessionId, otherSessionId, mockUrl } = backend;
      const targetUrl = `${serverUrl}/c/${otherSessionId}`;
      const scripted = await fetch(`${mockUrl}/mock/set_fallback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key: "gpt-4o-mini",
          text: `The other session is here: ${targetUrl}`,
          stream: true,
        }),
      });
      assert.equal(scripted.status, 200);

      desktop = await launchDesktop({ serverUrl, recordDir, preload: [FAKE_BROWSER] });
      const { electronApp, window } = desktop;
      await window.goto(`${serverUrl}/c/${sessionId}`);
      const composer = window.getByRole("textbox", { name: "Message the agent" });
      await composer.waitFor({ timeout: 30_000 });
      await composer.fill("Where is the other session?");
      await composer.press("Enter");

      const sessionLink = window.locator(`a[href="${targetUrl}"]`);
      await sessionLink.waitFor({ timeout: 60_000 });
      // Send replaces the Interrupt button once the reply has finished streaming.
      await window
        .getByTestId("composer-action-row")
        .getByRole("button", { name: "Send", exact: true })
        .waitFor({ timeout: 30_000 });
      await sessionLink.click();
      // The linked session is empty, so its welcome prompt shows it rendered in this window.
      // Read external opens only after that, so a late one still fails the assertion.
      const rendered = await window
        .getByText("What should we work on?")
        .waitFor({ timeout: 15_000 })
        .then(
          () => true,
          () => false,
        );
      const openedExternally = await electronApp.evaluate(() => globalThis.fakeBrowserOpened);
      assert.deepEqual(
        { openedExternally, windowPath: new URL(window.url()).pathname, rendered },
        { openedExternally: [], windowPath: `/c/${otherSessionId}`, rendered: true },
      );
      if (process.env.OMNIGENT_DESKTOP_RECORD_DIR) await window.waitForTimeout(2_000);
    } finally {
      // Stop the screen capture first so the clip ends on the opened session, not teardown.
      await desktop?.stopDisplayCapture();
      await desktop?.electronApp.close().catch(() => {});
      await backend?.close();
      console.log(saveRecording(recordDir, "session-link"));
      if (desktop) fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
