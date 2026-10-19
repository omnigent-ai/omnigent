"use strict";

const { before, after, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const {
  desktopDepsAvailable,
  launchDesktop,
  openBrowserFixture,
  saveRecording,
} = require("./desktopHarness");
const { eventually, startDesignBackend } = require("./desktopDesignPromptHarness");

// Like device-verification sign-in, query Chromium's permission before trying
// a real cross-origin loopback request. No permission responses are stubbed.
async function startDeviceSignIn() {
  let requests = 0;
  const server = http.createServer((request, response) => {
    if (request.url === "/device") {
      requests++;
      response.writeHead(200, { "Access-Control-Allow-Origin": origin });
      response.end("Connected to device");
      return;
    }
    response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
    response.end(`<!doctype html><html><meta charset="utf-8"><title>Device sign-in</title>
      <style>body{font:18px system-ui;padding:36px;color:#202b39;background:#f5f7fa}
      button{padding:12px;font:inherit}output{display:block;margin-top:24px}</style>
      <h1>Device sign-in</h1><p>Local network access is needed to contact the device.</p>
      <button>Check connection</button><output>Ready</output><script>
      async function check() {
        sessionStorage.setItem('checking', '1');
        const output = document.querySelector('output');
        const permission = await navigator.permissions.query({name:'loopback-network'});
        if (permission.state !== 'granted') {
          output.textContent = 'Local network access blocked. Change site permissions to continue.';
          return;
        }
        try { output.textContent = await (await fetch('http://localhost:${server.address().port}/device')).text(); }
        catch (error) { output.textContent = String(error); }
      }
      document.querySelector('button').onclick = check;
      if (sessionStorage.getItem('checking')) check();
      </script></html>`);
  });
  await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  return {
    url: `${origin}/signin`,
    requests: () => requests,
    close: () =>
      new Promise((resolve) => {
        server.close(resolve);
      }),
  };
}

async function permissionPrompt(electronApp) {
  const popup = await eventually(
    () =>
      electronApp.windows().find((page) => page.url().endsWith("/browser-permission/index.html")),
    "native local network permission prompt",
  );
  await popup.getByRole("button", { name: "Allow once", exact: true }).waitFor();
  return popup;
}

const deps = desktopDepsAvailable();
describe(
  "desktop browser — recover local network permission",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, concurrency: 1 },
  () => {
    let backend;
    let tmpDir;
    let recordRoot;

    before(
      async () => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-browser-permissions-"));
        recordRoot = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings");
        backend = await startDesignBackend(tmpDir);
      },
      { timeout: 120_000 },
    );

    after(async () => {
      await backend?.close();
    });

    for (const [initialChoice, recoveryChoice] of [
      ["Deny", "Allow once"],
      ["Dismiss permission request", "Always allow"],
    ]) {
      it(
        `recovers after ${initialChoice} through site permissions`,
        { timeout: 120_000 },
        async (t) => {
          const recordDir = path.join(recordRoot, initialChoice === "Deny" ? "deny" : "dismiss");
          const fixture = await startDeviceSignIn();
          let desktop;
          try {
            desktop = await launchDesktop({ serverUrl: backend.serverUrl, recordDir });
            const { electronApp, window } = desktop;
            const nativeWindow = await electronApp.browserWindow(window);
            await nativeWindow.evaluate((win) => {
              win.setSize(1400, 900);
              win.show();
              win.focus();
            });
            await window.goto(`${backend.serverUrl}/c/${backend.sessionId}`);
            await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
            const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
            if (await expand.isVisible()) await expand.click();
            await window.getByRole("button", { name: "Open new", exact: true }).click();
            await window.getByRole("menuitem", { name: "Browser", exact: true }).click();
            const pane = window.locator("[data-browser-pane-conversation]");
            const viewId = await pane.getAttribute("data-browser-pane-conversation");
            assert.ok(viewId);
            // Local fixture setup uses the existing main-process driver; the
            // browser's navigation allowlist and permission handlers stay intact.
            assert.equal((await openBrowserFixture(window, viewId, fixture.url)).ok, true);
            const page = await eventually(
              () => electronApp.windows().find((candidate) => candidate.url() === fixture.url),
              "embedded device sign-in page",
            );
            await page.getByRole("button", { name: "Check connection" }).click();
            const popup = await permissionPrompt(electronApp);
            await popup.getByRole("button", { name: initialChoice, exact: true }).click();
            await page.getByText("Local network access blocked.", { exact: false }).waitFor();
            assert.equal(fixture.requests(), 0, "denied queries must not reach the device");

            const permissions = pane.getByRole("button", { name: "Site permissions", exact: true });
            assert.equal(
              await permissions.count(),
              1,
              "a blocked site needs a way to reopen its permission decision",
            );
            await permissions.click();
            const retry = await permissionPrompt(electronApp);
            assert.equal(fixture.requests(), 0, "opening site settings must not grant access");
            await retry.screenshot({ path: path.join(recordDir, "permission-retry.png") });
            await retry.getByRole("button", { name: recoveryChoice, exact: true }).click();
            await page.getByText("Connected to device", { exact: true }).waitFor();
            assert.equal(
              fixture.requests(),
              1,
              "approval must reload and retry the real loopback request",
            );
            assert.equal(
              await page.evaluate(
                async () => (await navigator.permissions.query({ name: "loopback-network" })).state,
              ),
              "granted",
            );
            await window.screenshot({ path: path.join(recordDir, "toolbar.png") });
            await page.screenshot({ path: path.join(recordDir, "connected.png") });
            t.diagnostic(`Permission recovery recordings: ${recordDir}`);
          } finally {
            if (desktop) {
              await desktop.electronApp.close();
              await desktop.stopDisplayCapture();
              saveRecording(recordDir, "permission-recovery");
              fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
            }
            await fixture.close();
          }
        },
      );
    }
  },
);
