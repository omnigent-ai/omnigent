// Desktop-shell recording lane: private conversation cookies by default, with
// optional shared browser logins that survive an app restart.
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
  const result = await window.evaluate(
    (cid) => window.omnigentDesktop.browserSetActive(cid),
    conversationId,
  );
  assert.equal(result.ok, true, `activate ${conversationId} failed: ${result.error}`);
}

// Cookie updates can reach sibling renderers after the writer returns.
async function waitForIdentityCookie(window, conversationId, expected) {
  await window.waitForFunction(
    async ({ id, cookie }) => {
      const result = await window.omnigentDesktop.browserExecute(id, "document.cookie");
      const identity = String(result.result)
        .split(/;\s*/)
        .find((value) => value.startsWith("agent_identity="));
      return result.ok && (identity ?? null) === cookie;
    },
    { id: conversationId, cookie: expected },
    { timeout: 10_000 },
  );
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
  "desktop shell — browser login storage",
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
          const signIn = await execInView(
            window,
            "conv_A",
            `document.cookie = "agent_identity=alice; path=/";` +
              bannerJs("Conversation A", "Signed in as alice (cookie set in THIS view)", "#1a7f37"),
          );
          assert.equal(signIn.ok, true, `conv_A execute failed: ${signIn.error}`);
          assert.match(String(signIn.result), /agent_identity=alice/);
          await window.waitForTimeout(2_500); // hold A's state on film

          const firstTab = "browser-tab:conv_A:first";
          const secondTab = "browser-tab:conv_A:second";
          for (const tab of [firstTab, secondTab]) {
            // oxlint-disable-next-line no-await-in-loop -- Drive each native tab sequentially.
            const opened = await openView(window, tab, site);
            assert.equal(opened.ok, true, `${tab} open failed: ${opened.error}`);
            // oxlint-disable-next-line no-await-in-loop -- Wait for this tab's navigation.
            await waitForViewOnOrigin(window, tab, server.serverUrl);
            // oxlint-disable-next-line no-await-in-loop -- Read this tab's actual cookie jar.
            await waitForIdentityCookie(window, tab, "agent_identity=alice");
          }
          await setActive(window, secondTab);
          const changed = await execInView(
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
          const loggedOut = await execInView(
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

    it(
      "remembered logins share cookies and storage, survive restart, and can be disabled or cleared",
      { timeout: 180_000 },
      async (t) => {
        t.diagnostic(`Desktop recordings: ${recordDir}`);
        const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-saved-logins-"));
        const site = `${server.serverUrl}/health`;
        const first = "conv_A";
        const second = "browser-tab:conv_B:first";
        let desktop;
        const rememberLogins = (checked) =>
          desktop.window.getByRole("switch", {
            name: "Remember logins across sessions",
            exact: true,
            checked,
          });
        const settings = async (remembered) => {
          await desktop.window.goto(`${server.serverUrl}/settings/general`);
          await rememberLogins(remembered).waitFor({ state: "visible" });
        };
        const open = async (id) => {
          // Settings can suppress native browser views until we leave the route.
          if (new URL(desktop.window.url()).pathname === "/settings/general") {
            await desktop.window.goto(server.serverUrl);
            await desktop.window.getByText("What should we build?").waitFor({ state: "visible" });
          }
          const result = await openView(desktop.window, id, site);
          assert.equal(result.ok, true, `open ${id} failed: ${result.error}`);
          await waitForViewOnOrigin(desktop.window, id, server.serverUrl);
          await setActive(desktop.window, id);
        };
        const storage = async (id) => {
          const result = await execInView(
            desktop.window,
            id,
            'JSON.stringify([document.cookie, localStorage.getItem("agent_identity")])',
          );
          assert.equal(result.ok, true, `read ${id} failed: ${result.error}`);
          return JSON.parse(result.result);
        };
        try {
          desktop = await launchDesktop({ recordDir, serverUrl: server.serverUrl, userDataDir });
          await settings(false);
          await rememberLogins(false).click();
          await rememberLogins(true).waitFor({ state: "visible" });
          await open(first);
          const signIn = await execInView(
            desktop.window,
            first,
            'document.cookie = "agent_identity=alice; max-age=86400; path=/";' +
              'localStorage.setItem("agent_identity", "alice"); document.title = "Conversation A";' +
              bannerJs(
                "Conversation A",
                "Remember browser logins enabled — signed in as alice",
                "#1a7f37",
              ),
          );
          assert.equal(signIn.ok, true, `sign in failed: ${signIn.error}`);
          await open(second);
          await waitForIdentityCookie(desktop.window, second, "agent_identity=alice");
          assert.deepEqual(await storage(second), ["agent_identity=alice", "alice"]);
          await execInView(
            desktop.window,
            second,
            'document.title = "Conversation B";' +
              bannerJs(
                "Conversation B",
                "Already signed in as alice in this new session",
                "#0969da",
              ),
          );
          assert.deepEqual(await execInView(desktop.window, first, "document.title"), {
            ok: true,
            result: "Conversation A",
          });
          assert.deepEqual(
            await desktop.window.evaluate(() => [
              document.cookie.includes("agent_identity="),
              localStorage.getItem("agent_identity"),
            ]),
            [false, null],
            "embedded logins must not enter the Omnigent shell's session",
          );
          await desktop.window.waitForTimeout(1_500);
          await desktop.electronApp.close();
          await desktop.stopDisplayCapture();
          saveRecording(recordDir, "remember-browser-logins");
          desktop = undefined;

          // Reuse the profile without reseeding settings.json.
          desktop = await launchDesktop({ recordDir, userDataDir });
          await settings(true);
          await open(first);
          assert.deepEqual(await storage(first), ["agent_identity=alice", "alice"]);
          await execInView(
            desktop.window,
            first,
            bannerJs("After restarting Omnigent", "Still signed in as alice", "#1a7f37"),
          );
          await desktop.window.waitForTimeout(1_500);

          await settings(true);
          await rememberLogins(true).click();
          await rememberLogins(false).waitFor({ state: "visible" });
          await open(first);
          assert.deepEqual(await storage(first), ["", null]);
          const privateLogin = await execInView(
            desktop.window,
            first,
            'document.cookie = "agent_identity=private; path=/";' +
              'localStorage.setItem("agent_identity", "private"); document.cookie',
          );
          assert.equal(privateLogin.ok, true, `private sign in failed: ${privateLogin.error}`);
          await open(second);
          assert.deepEqual(await storage(second), ["", null]);

          await settings(false);
          const clearButton = desktop.window.getByRole("button", {
            name: "Clear saved browser data",
            exact: true,
          });
          const confirmation = desktop.window.getByRole("dialog", {
            name: "Clear saved browser data?",
            exact: true,
          });
          await clearButton.click();
          await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
          await confirmation.waitFor({ state: "hidden" });
          await rememberLogins(false).click();
          await rememberLogins(true).waitFor({ state: "visible" });
          await open(first);
          assert.deepEqual(await storage(first), ["agent_identity=alice", "alice"]);

          await settings(true);
          await rememberLogins(true).click();
          await rememberLogins(false).waitFor({ state: "visible" });
          await clearButton.click();
          await confirmation.getByRole("button", { name: "Clear data", exact: true }).click();
          await confirmation.waitFor({ state: "hidden" });
          await desktop.window.getByText("Saved browser data cleared.", { exact: true }).waitFor();
          await rememberLogins(false).click();
          await rememberLogins(true).waitFor({ state: "visible" });
          await open(first);
          assert.deepEqual(await storage(first), ["", null]);
          await execInView(
            desktop.window,
            first,
            bannerJs("Saved browser data cleared", "Signed out with empty site storage", "#0969da"),
          );
          await desktop.window.waitForTimeout(1_500);
        } finally {
          if (desktop) {
            await desktop.electronApp.close();
            await desktop.stopDisplayCapture();
            saveRecording(recordDir, "remember-browser-logins-restarted");
          }
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }
      },
    );
  },
);
