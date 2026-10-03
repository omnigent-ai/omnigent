// Desktop-shell journey for a self-hosted `oidc` server: the shell must hand the
// IdP sign-in to the system browser (RFC 8252 §8.12) instead of rendering it
// in-window. On Linux a scripted xdg-open completes the sign-in as the browser.
//
// Run from web/electron after building the SPA (see e2e/README.md):
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_oidc_in_window_idp.e2e.js

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
const { FAKE_IDP_EMAIL, startFakeIdp, oidcServerEnv } = require("./fixtures/fakeOidcIdp");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-oidc-in-window-idp");
const FAKE_BROWSER_DIR = path.join(__dirname, "fixtures", "fakeSystemBrowser");
const IDP_NAVIGATION_WINDOW_MS = 20_000;
const SIGN_IN_WINDOW_MS = 30_000;
// xdg-open is how Electron opens external links on Linux; elsewhere a real
// browser would open and the scripted completion cannot run.
const SIGN_IN_JOURNEY_SKIP =
  process.platform === "linux" ? false : "the scripted system browser replaces xdg-open (Linux)";
const PASSKEY_TIMEOUT_MS = 1_500;
// Far past both the RP timeout and Chromium's 10s floor for WebAuthn timeouts.
const PASSKEY_OBSERVATION_MS = 30_000;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

describe(
  "desktop shell — self-hosted OIDC sign-in",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let idp;
    let server;
    /** What the shell window did after Connect; filled once by the journey. */
    const observed = {
      windowUrls: [],
      inWindowIdpUrl: null,
      dialogSeen: false,
      idpAuthorizeAgents: [],
      windowCount: 0,
      webauthn: null,
      recordings: [],
      /** The second launch, whose system browser completes the sign-in. */
      signIn: {
        windowUrls: [],
        dialogSeen: false,
        dialogOpenAtEnd: null,
        finalUrl: null,
        meStatus: null,
        meUser: null,
        recordings: [],
      },
    };

    async function driveConnectJourney() {
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir: RECORD_DIR,
      });
      try {
        const urlField = window.locator("#url");
        await urlField.waitFor({ state: "visible", timeout: 15_000 });
        await urlField.fill(server.serverUrl);
        // Hold so the typed URL is legible in the recording before Connect.
        await sleep(2_500);
        await window.locator("#connect").click();

        const idpOrigin = new URL(idp.issuer).origin;
        const navigationDeadline = Date.now() + IDP_NAVIGATION_WINDOW_MS;
        /* oxlint-disable no-await-in-loop -- sequential observation of one window */
        while (Date.now() < navigationDeadline) {
          const url = window.url();
          if (observed.windowUrls.at(-1) !== url) observed.windowUrls.push(url);
          if (electronApp.windows().some((page) => page.url().endsWith("/oidc_login.html"))) {
            observed.dialogSeen = true;
          }
          if (url.startsWith(idpOrigin)) {
            observed.inWindowIdpUrl = url;
            break;
          }
          await sleep(250);
        }
        observed.idpAuthorizeAgents = idp.requests
          .filter((r) => r.path === "/authorize")
          .map((r) => r.userAgent);
        observed.windowCount = electronApp.windows().length;

        if (observed.inWindowIdpUrl) {
          const status = window.locator("#webauthn-status");
          await status.waitFor({ state: "visible", timeout: 10_000 });
          const started = Date.now();
          while (Date.now() - started < PASSKEY_OBSERVATION_MS) {
            let state;
            try {
              state = await status.getAttribute("data-state");
            } catch {
              break; // the page moved on
            }
            if (state !== "pending") break;
            await sleep(1_000);
          }
          observed.webauthn = await window
            .evaluate(() => {
              const ceremony = window.passkeyCeremony;
              if (!ceremony) return null;
              return {
                ...ceremony,
                observedMs: Date.now() - ceremony.startedAt,
                statusText: document.getElementById("webauthn-status")?.textContent ?? "",
              };
            })
            .catch(() => null);
          await window.screenshot({ path: path.join(RECORD_DIR, "in-window-idp-passkey.png") });
        }
        /* oxlint-enable no-await-in-loop */
      } finally {
        await electronApp.close().catch(() => {});
        await stopDisplayCapture();
        observed.recordings = saveRecording(RECORD_DIR, "before-oidc-in-window-idp");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    }

    async function driveSignInJourney() {
      const signIn = observed.signIn;
      fs.accessSync(path.join(FAKE_BROWSER_DIR, "xdg-open"), fs.constants.X_OK);
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir: RECORD_DIR,
        env: { PATH: `${FAKE_BROWSER_DIR}${path.delimiter}${process.env.PATH}` },
      });
      const dialogOpen = () =>
        electronApp.windows().some((page) => page.url().endsWith("/oidc_login.html"));
      try {
        const urlField = window.locator("#url");
        await urlField.waitFor({ state: "visible", timeout: 15_000 });
        await urlField.fill(server.serverUrl);
        await window.locator("#connect").click();

        const idpOrigin = new URL(idp.issuer).origin;
        const deadline = Date.now() + SIGN_IN_WINDOW_MS;
        /* oxlint-disable no-await-in-loop -- sequential observation of one window */
        while (Date.now() < deadline) {
          const url = window.url();
          if (signIn.windowUrls.at(-1) !== url) signIn.windowUrls.push(url);
          if (dialogOpen()) signIn.dialogSeen = true;
          if (url.startsWith(idpOrigin)) break;
          if (signIn.dialogSeen && !dialogOpen() && url.startsWith(server.serverUrl)) break;
          await sleep(250);
        }
        /* oxlint-enable no-await-in-loop */
        signIn.finalUrl = window.url();
        if (signIn.finalUrl.startsWith(server.serverUrl)) {
          const me = await window.evaluate(async () => {
            const response = await fetch("/v1/me", { credentials: "include" });
            return { status: response.status, body: await response.text() };
          });
          signIn.meStatus = me.status;
          try {
            signIn.meUser = JSON.parse(me.body).user_id ?? null;
          } catch {
            signIn.meUser = null;
          }
        }
        // Hold the signed-in app so the recording ends on it.
        await sleep(2_000);
        signIn.dialogOpenAtEnd = dialogOpen();
      } finally {
        await electronApp.close().catch(() => {});
        await stopDisplayCapture();
        signIn.recordings = saveRecording(RECORD_DIR, "after-oidc-system-browser-sign-in");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    }

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-oidc-"));
      idp = await startFakeIdp({ passkeyTimeoutMs: PASSKEY_TIMEOUT_MS });
      server = await spawnServer(tmpDir, {
        env: ({ serverUrl }) => oidcServerEnv(idp, serverUrl),
      });
      await driveConnectJourney();
      if (!SIGN_IN_JOURNEY_SKIP) await driveSignInJourney();
      fs.writeFileSync(
        path.join(RECORD_DIR, "observed.json"),
        JSON.stringify(
          { serverUrl: server.serverUrl, idpIssuer: idp.issuer, ...observed },
          null,
          2,
        ),
      );
    });

    after(async () => {
      if (server) await server.close();
      if (idp) await idp.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("keeps the third-party IdP sign-in page out of the shell window", () => {
      const w = observed.webauthn;
      const passkey = w
        ? `\nin-window navigator.credentials.get({ timeout: ${PASSKEY_TIMEOUT_MS} }) was ` +
          `${w.state} after ${w.observedMs} ms (isUserVerifyingPlatformAuthenticatorAvailable=` +
          `${w.uvpaa}; status: "${w.statusText}")`
        : "";
      assert.equal(
        observed.inWindowIdpUrl,
        null,
        `the Electron window itself navigated to the IdP: ${observed.inWindowIdpUrl}\n` +
          `window URLs after Connect: ${observed.windowUrls.join(" → ")}\n` +
          `IdP /authorize fetched by: ${observed.idpAuthorizeAgents.join(" | ") || "(nobody)"}` +
          passkey,
      );
      assert.equal(observed.dialogSeen, true, "the shell's sign-in dialog never appeared");
    });

    it(
      "completes sign-in through the system browser and loads the app signed in",
      { skip: SIGN_IN_JOURNEY_SKIP },
      () => {
        const s = observed.signIn;
        assert.equal(s.dialogSeen, true, "the shell's sign-in dialog never appeared");
        assert.ok(
          s.finalUrl?.startsWith(server.serverUrl),
          `the window never loaded the server after sign-in; URLs: ${s.windowUrls.join(" → ")}`,
        );
        assert.equal(s.meStatus, 200, `/v1/me from the loaded app returned ${s.meStatus}`);
        assert.equal(s.meUser, FAKE_IDP_EMAIL);
        assert.equal(s.dialogOpenAtEnd, false, "the sign-in dialog stayed open");
      },
    );
  },
);
