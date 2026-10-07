// Desktop e2e: an SSO-gated server whose IdP requires a security-key/biometric
// (WebAuthn) step. The shell must not render it in-window — Electron has no
// WebAuthn prompt UI, so the ceremony runs invisibly and strands the user.

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
const { startFakeIdp, startFakeFrontDoor } = require("./fixtures/fakeSsoFrontDoor");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-in-window-webauthn-sign-in");
const FAKE_BROWSER = path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs");
// A browser shows its security-key/biometric sheet within about a second.
const PROMPT_OBSERVATION_MS = 20_000;

const sleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

async function waitFor(check, message, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  // oxlint-disable no-await-in-loop -- polling.
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await sleep(250);
  }
  // oxlint-enable no-await-in-loop
  throw new Error(message);
}

const browserOpens = (electronApp) => electronApp.evaluate(() => globalThis.fakeBrowserOpened);

/** Every Electron window and whether a multi-account WebAuthn chooser was ever requested. */
const shellState = (electronApp) =>
  electronApp.evaluate(({ BrowserWindow }) => ({
    windows: BrowserWindow.getAllWindows().map((win) => ({
      url: win.webContents.getURL(),
      visible: win.isVisible(),
      child: win.getParentWindow() !== null,
    })),
    selectWebauthnAccount: globalThis.webauthnAccountChoosers ?? 0,
  }));

const countWebauthnAccountChoosers = (electronApp) =>
  electronApp.evaluate(({ session }) => {
    globalThis.webauthnAccountChoosers = 0;
    session.defaultSession.on("select-webauthn-account", (_event, _details, callback) => {
      globalThis.webauthnAccountChoosers += 1;
      callback();
    });
  });

describe(
  "desktop shell — SSO sign-in that needs a security key or biometric verification",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let idp;
    let frontDoor;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-webauthn-"));
      server = await spawnServer(tmpDir);
      idp = await startFakeIdp();
      frontDoor = await startFakeFrontDoor({ idp, upstreamUrl: server.serverUrl });
    });

    after(async () => {
      if (frontDoor) await frontDoor.close();
      if (idp) await idp.close();
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("completes the verification outside the app window instead of running it there invisibly", async () => {
      const home = path.join(tmpDir, "home");
      fs.mkdirSync(home, { recursive: true });
      const app = await launchDesktop({
        recordDir: RECORD_DIR,
        env: { HOME: home },
        preload: [FAKE_BROWSER],
      });
      const windowUrls = [];
      let observed = null;
      try {
        await countWebauthnAccountChoosers(app.electronApp);
        app.window.on("framenavigated", (frame) => {
          if (frame === app.window.mainFrame()) windowUrls.push(frame.url());
        });

        // Fresh install: the setup page. Type the server URL and Connect.
        const urlField = app.window.locator("#url");
        await urlField.waitFor({ state: "visible", timeout: 15_000 });
        await urlField.fill(frontDoor.url);
        await app.window.locator("#connect").click();

        // Sign-in either leaves for the system browser or the IdP renders in the window.
        const outcome = await waitFor(
          async () => {
            const opened = await browserOpens(app.electronApp);
            if (opened.length > 0) return { kind: "browser", opened };
            if (app.window.url().startsWith(idp.issuer)) return { kind: "in-window" };
            return null;
          },
          "neither the system browser nor the IdP's verification page appeared within 30 s",
          30_000,
        );

        if (outcome.kind === "in-window") {
          const started = Date.now();
          // oxlint-disable no-await-in-loop -- observe the window for the prompt.
          while (Date.now() - started < PROMPT_OBSERVATION_MS) {
            await sleep(1000);
            observed = {
              elapsedMs: Date.now() - started,
              webauthn: await app.window.evaluate(() => window.webauthnCeremony ?? null),
              status: await app.window.locator("#status").textContent(),
              browserOpened: await browserOpens(app.electronApp),
              ...(await shellState(app.electronApp)),
            };
            const settled = ["resolved", "rejected"].includes(observed.webauthn?.state);
            if (
              settled ||
              observed.browserOpened.length > 0 ||
              observed.selectWebauthnAccount > 0
            ) {
              break;
            }
          }
          // oxlint-enable no-await-in-loop
          await app.window.screenshot({
            path: path.join(RECORD_DIR, "in-window-verification.png"),
          });
        }

        assert.notEqual(
          outcome.kind,
          "in-window",
          "the desktop window rendered the IdP's security-key/biometric step itself; " +
            `observed after ${Math.round((observed?.elapsedMs ?? 0) / 1000)} s: ${JSON.stringify(observed)}`,
        );
        assert.ok(
          windowUrls.every((url) => !url.startsWith(idp.issuer)),
          `the app window loaded the IdP: ${JSON.stringify(windowUrls)}`,
        );
        // The browser the shell opened is where the security-key/biometric prompt belongs.
        await waitFor(
          () => idp.verifyRequests.some((request) => request.userAgent === "fake-system-browser"),
          "the system browser never reached the IdP's verification step",
          15_000,
        );
      } finally {
        await app.electronApp.close();
        await app.stopDisplayCapture();
        saveRecording(RECORD_DIR, "before-security-key-prompt");
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
    });
  },
);
