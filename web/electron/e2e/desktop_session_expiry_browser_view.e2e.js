// Desktop-shell recording lane: embedded browser view must stop painting when
// the workspace session expires and the shell reloads onto the sign-in page.
//
// Journey: boot the shell connected to a server that sits behind an SSO-style
// auth gate → a conversation opens the embedded browser pane (browser-use) →
// the gate's session expires (every API call now 303-redirects to its login
// page, exactly what the Databricks gate does after a VPN drop) → the shell
// auto-reloads the window, which lands on the login page → the native
// WebContentsView must detach instead of painting over the login page — and
// must not still cover the SPA after signing back in.
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_session_expiry_browser_view.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

// Chromium on Linux honors proxy environment variables, so a CI-injected
// HTTP(S)_PROXY would route the shell's requests to the localhost servers this
// test spawns through an external proxy and break the journey. Everything here
// is loopback-only; drop the proxy env before any child process is spawned.
delete process.env.http_proxy;
delete process.env.HTTP_PROXY;
delete process.env.https_proxy;
delete process.env.HTTPS_PROXY;
delete process.env.all_proxy;
delete process.env.ALL_PROXY;
delete process.env.no_proxy;
delete process.env.NO_PROXY;

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-session-expiry-browser-view");

// The fake workspace sign-in page the gate serves once the session expires.
// The anchor navigates back to "/" so "signing back in" is a plain click.
const LOGIN_PAGE = `<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Sign in</title></head>
  <body style="font-family: sans-serif; background: #0e1116; color: #e6edf3; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
    <div style="text-align: center;">
      <h1>Workspace sign-in required</h1>
      <p style="font-size: 18px">Your session has expired. Sign in again to continue.</p>
      <a id="signin" href="/" style="display: inline-block; padding: 12px 28px; background: #2da44e; color: white; border-radius: 6px; font-size: 18px; text-decoration: none;">Sign in</a>
    </div>
  </body>
</html>`;

/**
 * A minimal SSO-style auth gate in front of the omnigent server, mirroring how
 * a workspace gate behaves: while healthy it transparently proxies every
 * request (including WebSocket upgrades); once "expired" it answers every
 * request with a 303 redirect to its own login page. The shell's
 * session-expiry watcher keys on exactly that redirect shape.
 *
 * @param {string} upstreamUrl The real omnigent server, e.g. "http://127.0.0.1:1234".
 * @returns {Promise<{ url: string, setExpired: (v: boolean) => void, close: () => Promise<void> }>}
 */
function startAuthGate(upstreamUrl) {
  const upstream = new URL(upstreamUrl);
  const state = { expired: false, origin: "" };

  const server = http.createServer((req, res) => {
    if (state.expired) {
      if (req.url.split("?")[0].endsWith("/login.html")) {
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        res.end(LOGIN_PAGE);
        return;
      }
      res.writeHead(303, { location: `${state.origin}/login.html` });
      res.end();
      return;
    }
    const proxied = http.request(
      {
        hostname: upstream.hostname,
        port: Number(upstream.port),
        path: req.url,
        method: req.method,
        headers: { ...req.headers, host: upstream.host },
      },
      (upstreamRes) => {
        // Node's client already de-chunks the body; forwarding hop-by-hop
        // headers (transfer-encoding/connection) would corrupt the response.
        const headers = { ...upstreamRes.headers };
        delete headers["transfer-encoding"];
        delete headers.connection;
        res.writeHead(upstreamRes.statusCode, headers);
        upstreamRes.pipe(res);
      },
    );
    proxied.on("error", () => {
      if (!res.headersSent) res.writeHead(502);
      res.end();
    });
    req.pipe(proxied);
  });

  // Pipe WebSocket upgrades through untouched so the SPA's live streams work.
  server.on("upgrade", (req, socket, head) => {
    if (state.expired) {
      socket.destroy();
      return;
    }
    const upSock = net.connect(Number(upstream.port), upstream.hostname, () => {
      let raw = `${req.method} ${req.url} HTTP/1.1\r\n`;
      for (let i = 0; i < req.rawHeaders.length; i += 2) {
        const name = req.rawHeaders[i];
        const value = name.toLowerCase() === "host" ? upstream.host : req.rawHeaders[i + 1];
        raw += `${name}: ${value}\r\n`;
      }
      raw += "\r\n";
      upSock.write(raw);
      if (head && head.length > 0) upSock.write(head);
      socket.pipe(upSock);
      upSock.pipe(socket);
    });
    upSock.on("error", () => socket.destroy());
    socket.on("error", () => upSock.destroy());
  });

  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      state.origin = `http://127.0.0.1:${server.address().port}`;
      resolve({
        url: state.origin,
        setExpired: (v) => {
          state.expired = !!v;
        },
        close: () =>
          new Promise((done) => {
            // Long-lived streams (SSE) hold the server open; drop them first.
            if (typeof server.closeAllConnections === "function") server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}

/**
 * Resolve the SHELL window's Playwright page. `firstWindow()` can hand back an
 * auxiliary window (the find bar / overlay is its own BrowserWindow), so pick
 * the page whose URL is the http(s) app page, waiting for it to exist.
 */
async function shellWindow(electronApp, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const pages = electronApp.windows();
    const hit = pages.find((p) => /^https?:/.test(p.url()));
    if (hit) return hit;
    if (Date.now() > deadline) {
      throw new Error(
        `no http(s) shell window appeared — windows: ${pages.map((p) => p.url()).join(", ")}`,
      );
    }
    // oxlint-disable-next-line no-await-in-loop -- Poll until the window exists.
    await new Promise((resolve) => {
      setTimeout(resolve, 500);
    });
  }
}

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

// Poll until the view has actually LANDED on the target origin. Each probe is
// raced with a short timeout: executeJavaScript against a mid-navigation view
// can hang until (or past) the load, so a stuck probe is dropped and retried.
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

/**
 * Count the native child views attached to the SHELL window's contentView.
 * The embedded browser pane attaches exactly one WebContentsView while
 * painting; a detached/destroyed view leaves the count at its baseline.
 * The shell window is the one on an http(s) page — the app also opens
 * auxiliary file:// windows (e.g. the update overlay) that must not be probed.
 */
async function attachedChildViewCount(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }) => {
    const win = BrowserWindow.getAllWindows().find(
      (w) => !w.isDestroyed() && /^https?:/.test(w.webContents.getURL()),
    );
    if (!win) return -1;
    return win.contentView.children.length;
  });
}

describe(
  "desktop shell — session-expiry reload detaches the embedded browser view",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let gate;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir);
      gate = await startAuthGate(server.serverUrl);
    });

    after(async () => {
      if (gate) await gate.close();
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "logged-out reload does not leave the browser view painting over the window",
      { timeout: 300_000 },
      async () => {
        // Boot straight onto the gated server (the failure is past connect).
        const { electronApp, userDataDir } = await launchDesktop({
          recordDir: RECORD_DIR,
          serverUrl: gate.url,
        });
        let attachedOnLoginPage;
        let attachedAfterSignIn;
        let baseline;
        try {
          // firstWindow() may be an auxiliary shell surface; wait for the
          // real app window (the one on an http(s) URL).
          const window = await shellWindow(electronApp);
          await window
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 90_000 });
          await window.waitForFunction(
            () =>
              !!window.omnigentDesktop &&
              typeof window.omnigentDesktop.browserOpenOrNavigate === "function",
            null,
            { timeout: 20_000 },
          );
          baseline = await attachedChildViewCount(electronApp);

          // The conversation opens its embedded browser pane on a website.
          // Point the view at the upstream server directly: agent-view traffic
          // rides its own partition (not the gate), like a real external site.
          const opened = await openView(window, "conv_A", `${server.serverUrl}/health`);
          assert.equal(opened.ok, true, `browser view open failed: ${opened.error}`);
          await setActive(window, "conv_A");
          await waitForViewOnOrigin(window, "conv_A", server.serverUrl);
          const painted = await execInView(
            window,
            "conv_A",
            `document.body.innerHTML = '<div style="font: 28px sans-serif; padding: 40px; background: #1a7f37; color: white; height: 100vh;">' +
              '<h1>Embedded browser (browser-use)</h1>' +
              '<p style="font-size: 20px">Native browser view driven by the agent.</p></div>';
            "ok";`,
          );
          assert.equal(painted.ok, true, `browser view paint failed: ${painted.error}`);
          const attachedWithPane = await attachedChildViewCount(electronApp);
          assert.equal(
            attachedWithPane,
            baseline + 1,
            "embedded browser view should be attached while the pane is open",
          );
          await window.waitForTimeout(2_500); // hold the healthy state on film

          // The workspace session expires: from now on the gate answers every
          // API call with a 303 to its login page. The next SPA request trips
          // the shell's expiry watcher, which reloads the window.
          gate.setExpired(true);
          await window
            .evaluate(() => fetch("/api/health").catch(() => {}))
            .catch(() => {
              /* reload may tear down the context mid-fetch — expected */
            });
          await window
            .getByText("Workspace sign-in required")
            .waitFor({ state: "visible", timeout: 30_000 });
          await window.waitForTimeout(2_500); // hold the logged-out state on film
          attachedOnLoginPage = await attachedChildViewCount(electronApp);

          // Sign back in: the gate is healthy again and the login page's
          // button navigates back to the app.
          gate.setExpired(false);
          await window.locator("#signin").click();
          await window
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 30_000 });
          await window.waitForTimeout(2_500); // hold the signed-in state on film
          attachedAfterSignIn = await attachedChildViewCount(electronApp);
          // Diagnostics: both counts, so a partial failure is visible even
          // though the asserts below stop at the first mismatch.
          console.log(
            `[session-expiry-browser-view] baseline=${baseline} onLoginPage=${attachedOnLoginPage} afterSignIn=${attachedAfterSignIn}`,
          );
        } finally {
          await electronApp.close();
          saveRecording(RECORD_DIR, "before-expiry-overlay");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }

        // The reload tears down the SPA renderer without unmounting the
        // BrowserPane, so nothing detaches the native view — it keeps painting
        // over the login page, and over the SPA after signing back in.
        assert.equal(
          attachedOnLoginPage,
          baseline,
          "embedded browser view still attached after the session-expiry reload — it paints over the sign-in page",
        );
        assert.equal(
          attachedAfterSignIn,
          baseline,
          "embedded browser view still attached after signing back in — it keeps covering the app",
        );
      },
    );
  },
);
