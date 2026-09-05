// Desktop-shell recording lane: per-conversation browser-view cookie isolation.
//
// Journey: boot the shell connected to a local server → conversation A opens a
// browser view on a site and "signs in" (sets an identity cookie) → conversation
// B opens its own view on the SAME site → B must start signed out (no cookie).
// On the unfixed shell both views live on session.defaultSession, so B sees A's
// cookie; with per-conversation partitions B's jar starts empty.
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_cookie_isolation.e2e.js

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
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-cookie-isolation");

// Drive the preload-exposed browser APIs from the shell renderer. Each helper
// runs in the SPA window's context, where window.omnigentDesktop exists.
async function openView(window, conversationId, url) {
  return window.evaluate(
    ([cid, u]) =>
      window.omnigentDesktop.browserOpenOrNavigate(cid, u, {
        x: 40,
        y: 80,
        width: 900,
        height: 520,
      }),
    [conversationId, url],
  );
}

async function execInView(window, conversationId, js) {
  return window.evaluate(
    ([cid, code]) => window.omnigentDesktop.browserExecute(cid, code),
    [conversationId, js],
  );
}

async function setActive(window, conversationId) {
  return window.evaluate((cid) => window.omnigentDesktop.browserSetActive(cid), conversationId);
}

// Poll until the view has actually LANDED on the target origin — an execute
// against a view still on about:blank would read the wrong (empty) cookie jar
// and could false-pass the isolation assertion. Each probe is raced with a
// short timeout: executeJavaScript against a mid-navigation view can hang
// until (or past) the load, so a stuck probe is dropped and retried.
async function waitForViewOnOrigin(window, conversationId, origin, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  let last = "no probe completed";
  for (;;) {
    // oxlint-disable-next-line no-await-in-loop -- Poll each navigation probe sequentially.
    const r = await Promise.race([
      execInView(window, conversationId, "location.origin"),
      new Promise((resolve) => {
        setTimeout(() => resolve(null), 3_000);
      }),
    ]);
    if (r && r.ok && String(r.result).startsWith(origin)) return;
    if (r) last = JSON.stringify(r);
    if (Date.now() > deadline) {
      throw new Error(`view ${conversationId} never landed on ${origin} — last: ${last}`);
    }
    // oxlint-disable-next-line no-await-in-loop -- Wait before starting the next probe.
    await window.waitForTimeout(500);
  }
}

// Paint a full-page banner into a view so the recording shows the identity
// state a user would see ("signed in as A" vs "signed out").
function bannerJs(title, detail, color) {
  return `
    document.body.innerHTML = '<div style="font: 28px sans-serif; padding: 40px; background: ${color}; color: white; height: 100vh;">' +
      '<h1>${title}</h1><p style="font-size: 20px">${detail}</p>' +
      '<p style="font-size: 18px">document.cookie = "' + document.cookie + '"</p></div>';
    document.cookie;
  `;
}

describe(
  "desktop shell — per-conversation browser cookie isolation",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir);
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "conversation B's view does not inherit conversation A's login cookie",
      { timeout: 180_000 },
      async () => {
        // Launch on the bundled setup page and connect interactively (the same
        // proven journey as desktop_connect.e2e.js), then drive the browser APIs.
        const { electronApp, window, userDataDir } = await launchDesktop({
          recordDir: RECORD_DIR,
        });
        let saved;
        try {
          const urlField = window.locator("#url");
          await urlField.waitFor({ state: "visible", timeout: 15_000 });
          await urlField.fill(server.serverUrl);
          await window.locator("#connect").click();
          await window
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 20_000 });
          // The SPA is up — its preload bridge carries the browser APIs.
          await window.waitForFunction(
            () =>
              !!window.omnigentDesktop &&
              typeof window.omnigentDesktop.browserOpenOrNavigate === "function",
            null,
            { timeout: 20_000 },
          );
          const site = `${server.serverUrl}/health`;

          // Conversation A opens the site and "signs in" (sets an identity cookie).
          const openedA = await openView(window, "conv_A", site);
          assert.equal(openedA.ok, true, `conv_A open failed: ${openedA.error}`);
          await setActive(window, "conv_A");
          await waitForViewOnOrigin(window, "conv_A", server.serverUrl);
          const signIn = await execInView(
            window,
            "conv_A",
            `document.cookie = "agent_identity=alice; path=/";` +
              bannerJs("Conversation A", "Signed in as alice (cookie set in THIS view)", "#1a7f37"),
          );
          assert.equal(signIn.ok, true, `conv_A execute failed: ${signIn.error}`);
          assert.match(String(signIn.result), /agent_identity=alice/);
          await window.waitForTimeout(2_500); // hold A's state on film

          // Conversation B opens its own view on the same site.
          const openedB = await openView(window, "conv_B", site);
          assert.equal(openedB.ok, true, `conv_B open failed: ${openedB.error}`);
          await setActive(window, "conv_B");
          await waitForViewOnOrigin(window, "conv_B", server.serverUrl);
          const readB = await execInView(
            window,
            "conv_B",
            bannerJs(
              "Conversation B",
              "Fresh view on the same site — must start signed out",
              "#0969da",
            ),
          );
          assert.equal(readB.ok, true, `conv_B execute failed: ${readB.error}`);
          await window.waitForTimeout(2_500); // hold B's state on film

          // The proof: B's cookie jar does not contain A's identity.
          assert.ok(
            !String(readB.result).includes("agent_identity"),
            `conv_B inherited conv_A's cookie — jars are shared: "${readB.result}"`,
          );
        } finally {
          await electronApp.close();
          saved = saveRecording(RECORD_DIR, "after-cookie-isolation");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
        assert.ok(saved && saved.length > 0, "no desktop recording was produced");
      },
    );
  },
);
