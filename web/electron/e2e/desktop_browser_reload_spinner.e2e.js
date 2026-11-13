// The embedded browser's Reload icon spins until the reloaded page arrives.
// A localhost fixture holds each response, so the spinner is checked while the
// reload request is still pending. Run as described in e2e/README.md.

"use strict";

const { after, before, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, startDisplayCapture } = require("./desktopHarness");
const {
  eventually,
  launchThisMachineDesktop,
  openSessionBrowserTab,
  startDesignBackend,
} = require("./desktopDesignPromptHarness");

const deps = desktopDepsAvailable();
const RESPONSE_DELAY_MS = 3_000;

/** A localhost page server that holds every response for `RESPONSE_DELAY_MS`. */
async function startSlowPageFixture() {
  const state = { requests: 0, pending: 0 };
  const server = http.createServer((request, response) => {
    if (request.url !== "/") {
      response.writeHead(404).end();
      return;
    }
    state.requests += 1;
    state.pending += 1;
    const loadNumber = state.requests;
    setTimeout(() => {
      state.pending -= 1;
      response.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
      });
      response.end(`<!doctype html><title>Slow preview</title>
<body style="margin:0;padding:32px;font:22px system-ui;background:#f4f6fa;color:#17243a">
<h1>Slow preview</h1><p id="load" style="font-size:40px">Load #${loadNumber}</p>
<p>This server waits ${RESPONSE_DELAY_MS / 1000} seconds before every response.</p></body>`);
    }, RESPONSE_DELAY_MS);
  });
  await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    url: `http://localhost:${server.address().port}/`,
    state,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(resolve);
      }),
  };
}

describe(
  "desktop embedded browser reload feedback",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let scratch;
    let backend;
    let fixture;
    let desktop;
    let recordDir;
    let stopCapture = async () => {};

    before(
      async () => {
        scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-reload-spinner-"));
        recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(scratch, "recordings");
        fs.mkdirSync(recordDir, { recursive: true });
        fixture = await startSlowPageFixture();
        backend = await startDesignBackend(scratch);
        ({ desktop } = await launchThisMachineDesktop({
          backend,
          scratch,
          recordDir: path.join(scratch, "page-clips"),
          captureDisplay: false,
        }));
        const nativeWindow = await desktop.electronApp.browserWindow(desktop.window);
        await nativeWindow.evaluate((win) => {
          win.setBounds({ x: 0, y: 0, width: 1440, height: 900 });
          win.show();
          win.focus();
        });
        await eventually(
          () => desktop.window.url().startsWith(backend.serverUrl),
          "shell connected to the server",
        );
      },
      { timeout: 180_000 },
    );

    after(async () => {
      try {
        await stopCapture();
        await desktop?.electronApp.close();
      } finally {
        await Promise.all([fixture?.close(), backend?.close()]);
        if (desktop) fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
        if (scratch) fs.rmSync(scratch, { recursive: true, force: true });
      }
    });

    it("spins the Reload icon until the reloaded page arrives", { timeout: 120_000 }, async (t) => {
      const { window, electronApp } = desktop;
      // Wall-clock step times let a recording's captions match its footage.
      const mark = (step) => t.diagnostic(`${step} at ${Date.now()}`);
      const capture = startDisplayCapture(recordDir, process.env.DISPLAY);
      if (capture) stopCapture = capture.stop;

      mark("capture started");
      await openSessionBrowserTab(window, backend.serverUrl, backend.sessionId);
      const sidebar = window.getByRole("button", { name: "Close sidebar", exact: true });
      if (await sidebar.isVisible()) await sidebar.click();
      const pane = window.locator("[data-browser-pane-conversation]");
      const reload = pane.getByRole("button", { name: "Reload", exact: true });
      const spinning = pane.locator('button[aria-label="Reload"] svg.animate-spin');
      mark("browser tab open");
      await pane.getByRole("textbox", { name: "Address bar", exact: true }).click();
      await window.keyboard.type(fixture.url, { delay: 40 });
      await window.keyboard.press("Enter");
      mark("address submitted");

      const page = await eventually(
        () =>
          electronApp
            .context()
            .pages()
            .find((candidate) => candidate.url() === fixture.url),
        "embedded slow page",
      );
      await page.locator("#load", { hasText: "Load #1" }).waitFor({ timeout: 20_000 });
      await spinning.waitFor({ state: "detached", timeout: 10_000 });
      mark("first load shown");
      await window.waitForTimeout(2_000);

      await reload.click();
      await spinning.waitFor({ timeout: 2_000 });
      mark("reload clicked, spinning");
      assert.equal(fixture.state.pending, 1, "the reload request is still being answered");
      assert.equal(await reload.getAttribute("aria-busy"), "true");
      await window.waitForTimeout(1_500);
      assert.equal(await spinning.count(), 1, "the icon keeps spinning while the page loads");

      await page.locator("#load", { hasText: "Load #2" }).waitFor({ timeout: 20_000 });
      await spinning.waitFor({ state: "detached", timeout: 5_000 });
      mark("reloaded, stopped");
      assert.equal(fixture.state.pending, 0);
      assert.equal(await reload.getAttribute("aria-busy"), "false");
      await window.waitForTimeout(2_500);
    });
  },
);
