"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually } = require("./desktopDesignPromptHarness");
const {
  opensslAvailable,
  startFakeSecurityKeySignIn,
} = require("./fixtures/fakeSecurityKeySignIn");

const deps = desktopDepsAvailable();
const FAKE_BROWSER = path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs");
// The shell routes the apex like any Databricks App host.
const APP_HOST = "databricksapps.com";
const RECORDING = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);

// Holds each state on screen long enough to read in a recording.
const pause = () =>
  new Promise((resolve) => {
    setTimeout(resolve, RECORDING ? 4_000 : 0);
  });

it(
  "asks for the security key while an in-window sign-in page waits on one",
  {
    skip:
      (!deps.ok && `missing deps: ${deps.missing.join(", ")}`) ||
      (!opensslAvailable() && "openssl is required for the HTTPS front door") ||
      (process.platform === "win32" && "Windows draws its own WebAuthn dialog"),
    timeout: 120_000,
  },
  async () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-sso-security-key-"));
    const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(scratch, "recordings");
    const signIn = await startFakeSecurityKeySignIn({ scratch, appHost: APP_HOST });
    let desktop;
    try {
      desktop = await launchDesktop({ recordDir, preload: [signIn.preload, FAKE_BROWSER] });
      const { electronApp, window } = desktop;
      const requestState = () => window.evaluate(() => window.verifyState.outcome);
      const urlField = window.locator("#url");
      await urlField.waitFor({ state: "visible", timeout: 15_000 });
      await urlField.fill(signIn.appUrl);
      await pause();
      await window.locator("#connect").click();
      await eventually(
        async () => window.url() === signIn.verifyUrl && (await requestState()) === "pending",
        "the sign-in page's security-key request",
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
      assert.equal(await notice.locator("#origin").innerText(), new URL(signIn.verifyUrl).origin);
      assert.ok(await notice.locator("#other-method-hint").isVisible());
      assert.equal(
        await notice.getByRole("button", { name: "Open in browser" }).isVisible(),
        false,
        "a sign-in finished in another browser cannot sign the app window in",
      );
      assert.equal(await requestState(), "pending", "the notice leaves the request running");
      assert.equal(window.url(), signIn.verifyUrl);
      assert.deepEqual(await electronApp.evaluate(() => [...globalThis.fakeBrowserOpened]), []);
      await pause();
    } finally {
      try {
        if (desktop) {
          // Stop filming first so the clip ends on the notice, not on teardown.
          await desktop.stopDisplayCapture().catch(() => {});
          await desktop.electronApp.close().catch(() => {});
          console.log(saveRecording(recordDir, "sso-security-key-sign-in"));
          fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
        }
      } finally {
        await signIn.close();
        fs.rmSync(scratch, { recursive: true, force: true });
      }
    }
  },
);
