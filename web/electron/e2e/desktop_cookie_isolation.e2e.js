// Desktop-shell recording lane: shared cookies within a conversation, isolated
// cookies between conversations.
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
const { buildBrowserActionScript } = require("../src/browserActionScript.mjs");

const {
  desktopDepsAvailable,
  execInBrowserView,
  openBrowserFixture,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();

// Drive the preload-exposed browser APIs from the shell renderer. Each helper
// runs in the SPA window's context, where window.omnigentDesktop exists.
async function openView(window, conversationId, url) {
  return openBrowserFixture(window, conversationId, url);
}

async function setActive(window, conversationId) {
  const result = await window.evaluate(
    (cid) => window.omnigentDesktop.browserSetActive(cid),
    conversationId,
  );
  assert.equal(result.ok, true, `activate ${conversationId} failed: ${result.error}`);
}

// Cookie updates can reach sibling renderers after the writer returns.
async function waitForIdentityCookie(window, conversationId, expected) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    // oxlint-disable-next-line no-await-in-loop -- Wait for cookie propagation.
    const result = await execInBrowserView(window, conversationId, "document.cookie");
    const identity = String(result.result)
      .split(/;\s*/)
      .find((value) => value.startsWith("agent_identity="));
    if (result.ok && (identity ?? null) === expected) return;
    // oxlint-disable-next-line no-await-in-loop -- Wait for cookie propagation.
    await window.waitForTimeout(100);
  }
  throw new Error("Identity cookie did not propagate");
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
      execInBrowserView(window, conversationId, "location.origin"),
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
    let recordDir;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      recordDir =
        process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
        fs.mkdtempSync(path.join(os.tmpdir(), "desktop-cookie-isolation-"));
      server = await spawnServer(tmpDir);
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "performs snapshot, click and type through the preload bridge",
      { timeout: 180_000 },
      async () => {
        const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
          serverUrl: server.serverUrl,
          recordDir,
        });
        const conversationId = "browser-actions";
        try {
          await window
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 20_000 });
          await window.waitForFunction(() => !!window.omnigentDesktop?.browserPerformAction);
          const opened = await openView(window, conversationId, `${server.serverUrl}/health`);
          assert.equal(opened.ok, true, opened.error);
          await setActive(window, conversationId);
          await waitForViewOnOrigin(window, conversationId, new URL(server.serverUrl).origin);
          // Test setup and observation use main-process access; actions use the
          // production renderer -> preload -> IPC -> child WebContents path.
          const fixture = await execInBrowserView(
            window,
            conversationId,
            `
          document.body.innerHTML = '<button>Save</button><input aria-label="Name"><p id="saved"></p>';
          document.querySelector('button').addEventListener('click', () => {
            document.querySelector('#saved').textContent = document.querySelector('input').value;
          });
        `,
          );
          assert.equal(fixture.ok, true, fixture.error);
          /* oxlint-disable no-await-in-loop -- Both transports act sequentially on the same browser view. */
          for (const transport of ["typed", "legacy"]) {
            const perform = (action, params = {}) =>
              transport === "typed"
                ? window.evaluate(
                    ({ cid, action: requestedAction, params: requestedParams }) =>
                      window.omnigentDesktop.browserPerformAction(
                        cid,
                        requestedAction,
                        requestedParams,
                      ),
                    { cid: conversationId, action, params },
                  )
                : window.evaluate(
                    ({ cid, script }) => window.omnigentDesktop.browserExecute(cid, script),
                    { cid: conversationId, script: buildBrowserActionScript(action, params) },
                  );
            const snapshot = await perform("snapshot");
            assert.equal(snapshot.ok, true, snapshot.error);
            const { tree, snapshot_id: snapshotId } = JSON.parse(snapshot.result);
            const buttonRef = Number(tree.match(/button "Save" \[ref=(\d+)\]/)[1]);
            const inputRef = Number(tree.match(/textbox "Name" \[ref=(\d+)\]/)[1]);
            const typed = await perform("type", {
              ref: inputRef,
              snapshot_id: snapshotId,
              text: `Ada ${transport}`,
            });
            assert.equal(typed.ok, true, typed.error);
            const clicked = await perform("click", { ref: buttonRef, snapshot_id: snapshotId });
            assert.equal(clicked.ok, true, clicked.error);
            const observed = await execInBrowserView(
              window,
              conversationId,
              "document.querySelector('#saved').textContent",
            );
            assert.equal(observed.ok, true, observed.error);
            assert.equal(observed.result, `Ada ${transport}`);
          }
          /* oxlint-enable no-await-in-loop */
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saveRecording(recordDir, "browser-typed-actions");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
      },
    );

    it(
      "tabs share login changes within conversation A while conversation B stays signed out",
      { timeout: 180_000 },
      async (t) => {
        t.diagnostic(`Desktop recordings: ${recordDir}`);
        // Launch on the bundled setup page and connect interactively (the same
        // proven journey as desktop_connect.e2e.js), then drive the browser APIs.
        const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
          recordDir,
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
          const signIn = await execInBrowserView(
            window,
            "conv_A",
            `document.cookie = "agent_identity=alice; path=/";` +
              bannerJs("Conversation A", "Signed in as alice (cookie set in THIS view)", "#1a7f37"),
          );
          assert.equal(signIn.ok, true, `conv_A execute failed: ${signIn.error}`);
          assert.match(String(signIn.result), /agent_identity=alice/);
          await window.waitForTimeout(2_500); // hold A's state on film

          const blocked = await window.evaluate(
            (url) =>
              window.omnigentDesktop.browserOpenOrNavigate("restricted-fixture", url, undefined, {
                agent: false,
              }),
            server.serverUrl,
          );
          assert.equal(blocked.ok, false, "renderer navigation must enforce the URL policy");
          const firstTab = "browser-tab:conv_A:first";
          const secondTab = "browser-tab:conv_A:second";
          for (const tab of [firstTab, secondTab]) {
            // oxlint-disable-next-line no-await-in-loop -- Open each fixture before observing its cookies.
            const opened = await openView(window, tab, site);
            assert.equal(opened.ok, true, `${tab} open failed: ${opened.error}`);
            // oxlint-disable-next-line no-await-in-loop -- Wait for this tab's navigation.
            await waitForViewOnOrigin(window, tab, server.serverUrl);
            // oxlint-disable-next-line no-await-in-loop -- Read this tab's actual cookie jar.
            await waitForIdentityCookie(window, tab, "agent_identity=alice");
          }
          await setActive(window, secondTab);
          const changed = await execInBrowserView(
            window,
            secondTab,
            'document.cookie = "agent_identity=bob; path=/"; document.cookie',
          );
          assert.equal(changed.ok, true, `cookie update failed: ${changed.error}`);
          await waitForIdentityCookie(window, "conv_A", "agent_identity=bob");
          const closed = await window.evaluate(
            (id) => window.omnigentDesktop.browserClose(id),
            firstTab,
          );
          assert.equal(closed.ok, true, `tab close failed: ${closed.error}`);
          const reopened = await openView(window, firstTab, site);
          assert.equal(reopened.ok, true, `tab reopen failed: ${reopened.error}`);
          await waitForViewOnOrigin(window, firstTab, server.serverUrl);
          await waitForIdentityCookie(window, firstTab, "agent_identity=bob");

          // Conversation B opens its own view on the same site.
          const openedB = await openView(window, "conv_B", site);
          assert.equal(openedB.ok, true, `conv_B open failed: ${openedB.error}`);
          await setActive(window, "conv_B");
          await waitForViewOnOrigin(window, "conv_B", server.serverUrl);
          const readB = await execInBrowserView(
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
          const loggedOut = await execInBrowserView(
            window,
            secondTab,
            'document.cookie = "agent_identity=; max-age=0; path=/"; document.cookie',
          );
          assert.equal(loggedOut.ok, true, `logout failed: ${loggedOut.error}`);
          await waitForIdentityCookie(window, "conv_A", null);
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saved = saveRecording(recordDir, "after-cookie-isolation");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
        assert.ok(saved && saved.length > 0, "no desktop recording was produced");
      },
    );
  },
);
