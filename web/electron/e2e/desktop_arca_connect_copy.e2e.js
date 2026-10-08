// Desktop-shell regression lane: copying text out of the Arca connect console.
//
// Journey: a connected desktop window → host chip → "Run on Arca" → the
// shell-owned "Connect Arca" console shows the command; the user tries to copy
// from it (the command line, and after Connect the status line and the streamed
// output) by mouse selection, the copy shortcut, and right-click → Copy.
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 OMNIGENT_PYTHON=../../.venv/bin/python \
//     xvfb-run -a node --test e2e/desktop_arca_connect_copy.e2e.js
// The Databricks-only gates are stood in by fixtures/arcaFeatureGates.cjs and
// the arca CLI by fixtures/fakeArca.sh, which replays a sign-in-required run.

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
  path.join(__dirname, "recordings", "desktop-arca-connect-copy");
const GATES_FIXTURE = path.join(__dirname, "fixtures", "arcaFeatureGates.cjs");
const FAKE_ARCA = path.join(__dirname, "fixtures", "fakeArca.sh");
const COPY_SHORTCUT = process.platform === "darwin" ? "Meta+C" : "Control+C";
const SENTINEL = "clipboard-untouched";

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

describe(
  "desktop shell — copying from the Arca connect console",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let app;
    let saved;
    /** Facts gathered while driving the journey once; the tests assert on them. */
    const seen = {};

    const readClipboard = () => app.electronApp.evaluate(({ clipboard }) => clipboard.readText());
    const resetClipboard = () =>
      app.electronApp.evaluate(({ clipboard }, text) => clipboard.writeText(text), SENTINEL);
    const selectionText = (page) => page.evaluate(() => window.getSelection().toString());
    const resetContextMenus = () =>
      app.electronApp.evaluate(() => {
        globalThis.recordedContextMenus = [];
      });
    const contextMenus = () => app.electronApp.evaluate(() => globalThis.recordedContextMenus);

    async function dragAcross(page, locator) {
      const box = await locator.boundingBox();
      await page.mouse.move(box.x + 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width - 4, box.y + box.height / 2, { steps: 12 });
      await page.mouse.up();
    }

    async function rightClickAt(page, locator) {
      const box = await locator.boundingBox();
      await resetContextMenus();
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, { button: "right" });
      await sleep(1500);
      return contextMenus();
    }

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-arca-copy-"));
      server = await spawnServer(tmpDir);
      const bin = path.join(tmpDir, "bin");
      fs.mkdirSync(bin);
      fs.copyFileSync(FAKE_ARCA, path.join(bin, "arca"));
      fs.chmodSync(path.join(bin, "arca"), 0o755);
      app = await launchDesktop({
        recordDir: RECORD_DIR,
        serverUrl: server.serverUrl,
        preload: [GATES_FIXTURE],
        env: { PATH: `${bin}:${process.env.PATH}`, OMNIGENT_E2E_ARCA_SERVER_URL: server.serverUrl },
      });
      const { electronApp, window } = app;
      await window
        .getByText("What should we build?")
        .waitFor({ state: "visible", timeout: 40_000 });
      // Native menus never reach the page, so observe them where they are built.
      await electronApp.evaluate(({ Menu }) => {
        const popup = Menu.prototype.popup;
        globalThis.recordedContextMenus = [];
        Menu.prototype.popup = function (options) {
          globalThis.recordedContextMenus.push(this.items.map((item) => item.role ?? item.label));
          setTimeout(() => this.closePopup(), 1200);
          return popup.call(this, options);
        };
      });

      await window.locator('button[aria-label^="Host:"]').click();
      const consoleOpened = electronApp.waitForEvent("window", { timeout: 15_000 });
      await window.locator('[data-testid="new-chat-landing-run-on-arca"]').click();
      const consolePage = await consoleOpened;
      const command = consolePage.locator("#command");
      await consolePage.waitForFunction(
        () => /isaac omni host/.test(document.getElementById("command").textContent),
        null,
        { timeout: 15_000 },
      );
      seen.command = await command.textContent();
      await sleep(1000);

      await resetClipboard();
      await command.click({ clickCount: 3 });
      seen.commandSelection = await selectionText(consolePage);
      await consolePage.keyboard.press(COPY_SHORTCUT);
      await sleep(500);
      seen.commandClipboard = await readClipboard();
      seen.commandContextMenus = await rightClickAt(consolePage, command);

      await resetClipboard();
      await consolePage.locator("#confirm").click();
      await consolePage.waitForFunction(
        () => /OMNIGENT_AUTH_REQUIRED/.test(document.getElementById("terminal").textContent),
        null,
        { timeout: 20_000 },
      );
      await consolePage.waitForFunction(
        () =>
          document.getElementById("status").textContent.trim() !== "" &&
          !document.getElementById("confirm").classList.contains("loading"),
        null,
        { timeout: 20_000 },
      );
      await sleep(1000);
      const status = consolePage.locator("#status");
      seen.status = (await status.textContent()).trim();
      await status.click({ clickCount: 3 });
      seen.statusSelection = await selectionText(consolePage);
      await dragAcross(consolePage, status);
      seen.statusDragSelection = await selectionText(consolePage);
      await consolePage.keyboard.press(COPY_SHORTCUT);
      await sleep(500);
      seen.statusClipboard = await readClipboard();
      seen.statusContextMenus = await rightClickAt(consolePage, status);

      await resetClipboard();
      const errorRow = consolePage
        .locator(".xterm-rows > div", { hasText: "OMNIGENT_AUTH_REQUIRED" })
        .first();
      await dragAcross(consolePage, errorRow);
      await sleep(300);
      seen.terminalSelected = await consolePage.evaluate(
        () => document.querySelectorAll(".xterm-selection div").length > 0,
      );
      await consolePage.keyboard.press(COPY_SHORTCUT);
      await sleep(500);
      seen.terminalClipboard = await readClipboard();
      seen.terminalContextMenus = await rightClickAt(consolePage, errorRow);
      await sleep(1000);
    });

    after(async () => {
      if (app) {
        await app.electronApp.close();
        await app.stopDisplayCapture();
        saved = saveRecording(RECORD_DIR, "arca-console-copy");
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });

    it("copies the command line with mouse selection and the copy shortcut", () => {
      assert.match(seen.command, /isaac omni host --server/);
      assert.equal(seen.commandSelection, seen.command);
      assert.equal(seen.commandClipboard, seen.command);
    });

    it("offers a right-click Copy menu over text in the console", () => {
      const hasCopy = (menus) => menus.some((items) => items.includes("copy"));
      assert.ok(
        hasCopy(seen.commandContextMenus),
        `no context menu over the selected command: ${JSON.stringify(seen.commandContextMenus)}`,
      );
      assert.ok(hasCopy(seen.statusContextMenus), "no context menu over the status text");
      assert.ok(hasCopy(seen.terminalContextMenus), "no context menu over the terminal output");
    });

    it("lets the sign-in/failure status text be selected and copied", () => {
      assert.ok(seen.status.length > 0);
      assert.ok(
        seen.statusSelection.length > 0 || seen.statusDragSelection.length > 0,
        `status text could not be selected: ${JSON.stringify(seen.status)}`,
      );
      assert.ok(
        seen.statusClipboard.includes(seen.status),
        `clipboard after copying the status: ${JSON.stringify(seen.statusClipboard)}`,
      );
    });

    it("copies the selected terminal output with the copy shortcut", () => {
      assert.equal(seen.terminalSelected, true, "terminal output could not be selected");
      assert.match(seen.terminalClipboard, /OMNIGENT_AUTH_REQUIRED/);
    });
  },
);
