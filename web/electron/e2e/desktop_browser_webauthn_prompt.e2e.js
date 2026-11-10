"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { desktopDepsAvailable, saveRecording } = require("./desktopHarness");
const { startDesignBackend, eventually } = require("./desktopDesignPromptHarness");
const { launchThisMachineDesktop, openSessionBrowserTab } = require("./desktopBrowserTabHarness");

const deps = desktopDepsAvailable();
const FAKE_BROWSER = path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs");
const RECORDING = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);

// Holds each state on screen long enough to read in a recording.
const pause = () =>
  new Promise((resolve) => {
    setTimeout(resolve, RECORDING ? 3_000 : 0);
  });

// An Okta-style factor page that requests an assertion as soon as it loads.
const VERIFY_PAGE = `<!doctype html><title>Sign In</title>
<h2>Verify with Security Key or Biometric Authenticator</h2>
<p id="status">You will be prompted to use a security key or biometric verification.</p>
<script>
window.verifyState = { outcome: "idle" };
setTimeout(async () => {
  window.verifyState.outcome = "pending";
  try {
    await navigator.credentials.get({ publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId: "localhost",
      allowCredentials: [{ type: "public-key", id: crypto.getRandomValues(new Uint8Array(16)) }],
      userVerification: "preferred",
      timeout: 60000,
    } });
    window.verifyState.outcome = "signed-in";
  } catch (error) {
    window.verifyState.outcome = error.name;
  }
  document.getElementById("status").textContent = "Result: " + window.verifyState.outcome;
}, 500);
</script>`;

it(
  "asks for the security key while a Browser tab page waits and can hand the page to the system browser",
  {
    skip:
      (!deps.ok && `missing deps: ${deps.missing.join(", ")}`) ||
      (process.platform === "win32" && "Windows draws its own WebAuthn dialog"),
    timeout: 150_000,
  },
  async () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-webauthn-"));
    const recordDir = RECORDING
      ? process.env.OMNIGENT_DESKTOP_RECORD_DIR
      : fs.mkdtempSync(path.join(os.tmpdir(), "omni-webauthn-recordings-"));
    const fixture = http.createServer((_request, response) => {
      response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
      response.end(VERIFY_PAGE);
    });
    await new Promise((resolve) => {
      fixture.listen(0, "127.0.0.1", resolve);
    });
    // WebAuthn needs a secure context; loopback `localhost` is one.
    const verifyUrl = `http://localhost:${fixture.address().port}/okta/verify`;
    let backend;
    let desktop;
    try {
      backend = await startDesignBackend(scratch);
      ({ desktop } = await launchThisMachineDesktop({
        backend,
        scratch,
        recordDir,
        preload: [FAKE_BROWSER],
      }));
      const { window, electronApp } = desktop;
      await openSessionBrowserTab(window, backend.serverUrl, backend.sessionId);
      const bar = window.getByRole("textbox", { name: "Address bar" });
      await bar.fill(verifyUrl);
      await bar.press("Enter");
      const requestState = () =>
        electronApp.evaluate(async (_electron, url) => {
          const registry = global.omnigentTestBrowserRegistries[0];
          const contents = registry.get(registry.activeConversationId())?.view.webContents;
          return contents?.getURL() === url
            ? await contents.executeJavaScript("window.verifyState.outcome").catch(() => null)
            : null;
        }, verifyUrl);
      await eventually(
        async () => (await requestState()) === "pending",
        "the verify page's WebAuthn request",
      );

      const notice = await eventually(
        () =>
          electronApp
            .windows()
            .find((page) => page.url().endsWith("/browser-permission/index.html")),
        "the security-key notice",
        15_000,
      );
      await notice.getByRole("heading", { name: "Use your security key" }).waitFor();
      assert.equal(await notice.locator("#origin").innerText(), new URL(verifyUrl).origin);
      assert.equal(await requestState(), "pending", "the notice leaves the request running");
      await pause();

      await notice.getByRole("button", { name: "Open in browser" }).click();
      await eventually(() => notice.isClosed(), "the notice to close");
      assert.deepEqual(await electronApp.evaluate(() => [...globalThis.fakeBrowserOpened]), [
        verifyUrl,
      ]);
      await pause();
    } finally {
      try {
        if (desktop) {
          await desktop.window.unrouteAll({ behavior: "wait" }).catch(() => {});
          await desktop.electronApp.close().catch(() => {});
          await desktop.stopDisplayCapture().catch(() => {});
          console.log(saveRecording(recordDir, "browser-webauthn-prompt"));
          fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
        }
      } finally {
        if (backend) await backend.close();
        fixture.closeAllConnections();
        await new Promise((resolve) => {
          fixture.close(resolve);
        });
        fs.rmSync(scratch, { recursive: true, force: true });
      }
    }
  },
);
