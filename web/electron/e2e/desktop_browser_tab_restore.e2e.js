// Desktop-shell recording lane: open browser tabs survive a relaunch.
//
// Journey: open a session in the real SPA → open a Browser tab and load a page
// from the URL bar → quit → relaunch on the same userData → the tab is still
// listed under its page title and reopens that page when selected.
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_browser_tab_restore.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually, startDesignBackend } = require("./desktopDesignPromptHarness");

const deps = desktopDepsAvailable();
const PAGE_TITLE = "Restored tab fixture";

async function startTitledPage() {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(`<!doctype html><title>${PAGE_TITLE}</title><h1>${PAGE_TITLE}</h1>`);
  });
  await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    url: `http://127.0.0.1:${server.address().port}/restored`,
    close: () =>
      new Promise((resolve) => {
        server.close(resolve);
      }),
  };
}

async function openSession(window, serverUrl, sessionId) {
  await window.goto(`${serverUrl}/c/${sessionId}`);
  await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
  const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
  if (await expand.isVisible()) await expand.click();
}

/** The embedded WebContentsView currently showing `url`, if any. */
function embeddedPage(electronApp, url) {
  return electronApp
    .context()
    .pages()
    .find((page) => page.url() === url);
}

describe(
  "desktop shell — browser tabs restore after a relaunch",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let recordDir;
    let backend;
    let page;

    before(
      async () => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-tab-restore-"));
        recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings");
        page = await startTitledPage();
        backend = await startDesignBackend(tmpDir);
      },
      { timeout: 180_000 },
    );

    after(async () => {
      await Promise.all([page?.close(), backend?.close()]);
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "lists the tab under its page title and reopens its page",
      { timeout: 240_000 },
      async () => {
        const first = await launchDesktop({ recordDir, serverUrl: backend.serverUrl });
        const { userDataDir } = first;
        try {
          try {
            const { window, electronApp } = first;
            await openSession(window, backend.serverUrl, backend.sessionId);
            await window.getByRole("button", { name: "Open new", exact: true }).click();
            await window.getByRole("menuitem", { name: "Browser", exact: true }).click();
            const address = window.getByRole("textbox", { name: "Address bar", exact: true });
            await address.fill(page.url);
            await address.press("Enter");
            await eventually(() => embeddedPage(electronApp, page.url), "embedded page");
            await window.getByRole("tab", { name: PAGE_TITLE }).waitFor();
          } finally {
            await first.electronApp.close();
            await first.stopDisplayCapture();
            saveRecording(recordDir, "browser-tab-before-relaunch");
          }

          const second = await launchDesktop({
            recordDir,
            serverUrl: backend.serverUrl,
            userDataDir,
          });
          try {
            const { window, electronApp } = second;
            await openSession(window, backend.serverUrl, backend.sessionId);
            const tab = window.getByRole("tab", { name: PAGE_TITLE });
            await tab.waitFor();
            await tab.click();
            await eventually(() => embeddedPage(electronApp, page.url), "restored embedded page");
            assert.equal(
              await window.getByRole("textbox", { name: "Address bar", exact: true }).inputValue(),
              page.url,
            );
          } finally {
            await second.electronApp.close();
            await second.stopDisplayCapture();
            saveRecording(recordDir, "browser-tab-after-relaunch");
          }
        } finally {
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
      },
    );
  },
);
