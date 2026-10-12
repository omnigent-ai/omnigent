// Desktop e2e: OIDC sign-in through a stand-in system browser (see
// fixtures/fakeSystemBrowser.cjs). Proves the app window never reaches the IdP,
// and that the session renews, survives a relaunch, and stays signed out.

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
const { startFakeOidcIdp, oidcServerEnv } = require("./fixtures/fakeOidcIdp");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-oidc-browser-sign-in");
const COOKIE = "ap_session";

const FAKE_BROWSER = path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs");

const browserOpens = (electronApp) => electronApp.evaluate(() => globalThis.fakeBrowserOpened);
const launch = (userDataDir, home) =>
  launchDesktop({
    recordDir: RECORD_DIR,
    userDataDir,
    env: { HOME: home },
    preload: [FAKE_BROWSER],
  });

const sessionCookie = (electronApp, serverUrl) =>
  electronApp.evaluate(
    async ({ session }, [url, name]) =>
      (await session.defaultSession.cookies.get({ url, name }))[0]?.value ?? null,
    [serverUrl, COOKIE],
  );

async function waitFor(check, message, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  // oxlint-disable no-await-in-loop -- polling.
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise((resolve) => {
      setTimeout(resolve, 200);
    });
  }
  // oxlint-enable no-await-in-loop
  throw new Error(message);
}

describe(
  "desktop shell — OIDC sign-in through the system browser",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let idp;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-oidc-"));
      idp = await startFakeOidcIdp();
      server = await spawnServer(tmpDir, { env: (url) => oidcServerEnv(idp, url) });
    });

    after(async () => {
      if (server) await server.close();
      if (idp) await idp.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("signs in outside the app, renews silently, survives a relaunch, and signs out", async () => {
      // A scratch HOME keeps the refresh grant (~/.omnigent) out of the real one.
      const home = path.join(tmpDir, "home");
      fs.mkdirSync(home, { recursive: true });
      const userDataDir = path.join(tmpDir, "user-data");
      const host = new URL(server.serverUrl).host;
      const windowUrls = [];
      const saved = [];

      // 1. Connect: the browser signs in, the app never shows the IdP.
      let app = await launch(userDataDir, home);
      try {
        app.window.on("framenavigated", (frame) => windowUrls.push(frame.url()));
        const urlField = app.window.locator("#url");
        await urlField.waitFor({ state: "visible", timeout: 15_000 });
        await urlField.fill(server.serverUrl);
        await app.window.locator("#connect").click();
        await app.window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });

        const [opened] = await browserOpens(app.electronApp);
        assert.ok(opened, "the shell signed in without opening the system browser");
        const login = new URL(opened);
        assert.equal(`${login.origin}${login.pathname}`, `${server.serverUrl}/auth/login`);
        assert.equal(new URL(login.searchParams.get("native_redirect_uri")).hostname, "127.0.0.1");
        assert.equal(idp.authorizeRequests.length, 1);
        assert.equal(idp.authorizeRequests[0].userAgent, "fake-system-browser");
        assert.ok(
          windowUrls.every((url) => !url.startsWith(idp.issuer)),
          `the app window loaded the IdP: ${JSON.stringify(windowUrls)}`,
        );
        const me = await app.window.evaluate(() => fetch("/v1/me").then((r) => r.json()));
        assert.equal(me.user_id, idp.email);
        assert.ok(
          fs.existsSync(path.join(home, ".omnigent", "oidc_tokens.json")),
          "the refresh grant was not stored",
        );

        // 2. The session cookie disappears: the shell renews it without a browser.
        const first = await sessionCookie(app.electronApp, server.serverUrl);
        await app.electronApp.evaluate(
          ({ session }, [url, name]) => session.defaultSession.cookies.remove(url, name),
          [server.serverUrl, COOKIE],
        );
        const renewed = await waitFor(
          () => sessionCookie(app.electronApp, server.serverUrl),
          "the session cookie was not renewed",
        );
        assert.notEqual(renewed, first);
        assert.equal((await browserOpens(app.electronApp)).length, 1);
      } finally {
        // Name each launch's clips as it ends, so a failure keeps its footage.
        await app.electronApp.close();
        await app.stopDisplayCapture();
        saved.push(...saveRecording(RECORD_DIR, "oidc-sign-in-connect"));
      }

      // 3. Relaunch: the saved server opens signed in, still without a browser.
      app = await launch(userDataDir, home);
      try {
        await app.window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        assert.deepEqual(await browserOpens(app.electronApp), []);
        assert.equal(idp.authorizeRequests.length, 1);

        // 4. Sign out from the app: the connect screen says so, and it sticks.
        await app.window.evaluate(() => {
          window.location.href = "/auth/logout";
        });
        const message = app.window.locator("#err");
        await message.waitFor({ state: "visible", timeout: 15_000 });
        assert.equal(await message.textContent(), `You're signed out of ${host}.`);
        assert.equal(await sessionCookie(app.electronApp, server.serverUrl), null);
        const tokens = JSON.parse(
          fs.readFileSync(path.join(home, ".omnigent", "oidc_tokens.json"), "utf8"),
        );
        assert.deepEqual(tokens, {});
        assert.deepEqual(await browserOpens(app.electronApp), []);
      } finally {
        await app.electronApp.close();
        await app.stopDisplayCapture();
        saved.push(...saveRecording(RECORD_DIR, "oidc-sign-in-relaunch"));
      }
      assert.ok(saved.length > 0, "no desktop recording was produced");
    });
  },
);
