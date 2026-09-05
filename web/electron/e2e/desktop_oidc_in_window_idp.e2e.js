// Regression guard: the desktop shell must not render a third-party IdP
// sign-in page inside the Electron window (RFC 8252 §8.12 — embedded
// user-agent), and an IdP passkey (WebAuthn) stage must never hang forever.
//
// Journey (all user-observable):
//   1. self-host an omnigent server with OMNIGENT_AUTH_PROVIDER=oidc whose
//      IdP sign-in flow includes a passkey (WebAuthn) validation stage
//   2. open the desktop app -> bundled setup page
//   3. type the server URL -> Connect
//   4. regression: the Electron window ITSELF navigates to the third-party
//      IdP's sign-in page (embedded user-agent; RFC 8252 §8.12 forbids this)
//
// The fake IdP below is a real HTTP server (like tests/e2e_ui/auth/_fake_idp.py,
// in Node so this lane stays self-contained). Its /authorize page mimics
// authentik's WebAuthn Authenticator Validation stage: a normal modal
// credentials.get() with no mediation field and allowCredentials: [] (a
// discoverable-credential request), reporting ceremony state into the DOM so
// both the assertions and the recorded clip can see it.
//
// The test asserts the correct behavior: the IdP must not render in-window.
// If it does, the test also captures whether its modal ceremony settles. This
// covers loopback server URLs too: a tunneled/port-forwarded OIDC deployment
// must get the same handoff as a remote one.
//
// Run from web/electron, after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_oidc_in_window_idp.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const {
  REPO_ROOT,
  WEB_UI_DIST,
  desktopDepsAvailable,
  findFreePort,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

/** Repo-root recordings dir (workspace artifact, uncommitted). */
const RECORDINGS_ROOT = path.join(REPO_ROOT, "recordings", "desktop-oidc-idp");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

/** Everything in this journey is loopback (IdP, server, mock LLM). A CI
 * sandbox that exports HTTP(S)_PROXY with no no_proxy would otherwise route
 * the server's boot-time OIDC discovery fetch (httpx) through the proxy and
 * 502 it, so loopback is excluded explicitly for every spawned process. */
const NO_PROXY_LOOPBACK = [process.env.NO_PROXY || process.env.no_proxy, "localhost,127.0.0.1"]
  .filter(Boolean)
  .join(",");

/** PYTHONPATH for the spawned server: the repo root plus the in-repo SDK
 * packages the server imports (omnigent_client lives in sdks/python-client),
 * as ABSOLUTE paths — the spawned child's cwd is web/electron, so a relative
 * ambient PYTHONPATH would not resolve there. */
const SERVER_PYTHONPATH = [
  REPO_ROOT,
  path.join(REPO_ROOT, "sdks", "python-client"),
  path.join(REPO_ROOT, "sdks", "ui"),
].join(path.delimiter);

function testDepsAvailable() {
  const missing = [...desktopDepsAvailable().missing];
  if (!fs.existsSync(path.join(WEB_UI_DIST, "index.html"))) missing.push("built SPA");
  const python = spawnSync(PYTHON, ["-c", "from omnigent.cli import main"], {
    env: { ...process.env, PYTHONPATH: SERVER_PYTHONPATH },
    stdio: "ignore",
  });
  if (python.error || python.status !== 0) missing.push(`${PYTHON} with server deps`);
  return { ok: missing.length === 0, missing };
}

const deps = testDepsAvailable();

/** Same minimal agent spec as desktopHarness / the Python suite's fixture. */
const TEST_AGENT_YAML = `name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
`;

/** The RP-supplied WebAuthn timeout the fake IdP page uses — the same value
 * the bug report measured with ("a normal get() with timeout: 1500 remained
 * pending beyond 10s"). */
const RP_TIMEOUT_MS = 1500;

/** How long the ceremony gets to settle before we call it hung. Matches the
 * report's ">10s" observation window (~7x the RP timeout). */
const SETTLE_DEADLINE_MS = 10_000;

const HEALTH_TIMEOUT_MS = 60_000;
const HEALTH_POLL_MS = 500;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

const SUITE_T0 = Date.now();

/** Timestamped progress log (elapsed since suite start), for diagnosing
 * where wall time goes when the run is driven under a tight CI window. */
function logStep(msg) {
  console.log(`[oidc-idp +${((Date.now() - SUITE_T0) / 1000).toFixed(1)}s] ${msg}`);
}

/** GET a URL, resolving the status code (or rejecting on connect error). */
function httpStatus(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
    req.on("error", reject);
    req.setTimeout(2000, () => req.destroy(new Error("timeout")));
  });
}

async function waitForHealthy(url, label, logPath) {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  let lastError = "not polled yet";
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    try {
      if ((await httpStatus(url)) === 200) return;
    } catch (err) {
      lastError = `${err && err.code ? err.code : err}`;
    }
    await sleep(HEALTH_POLL_MS);
  }
  /* oxlint-enable no-await-in-loop */
  const log = logPath && fs.existsSync(logPath) ? fs.readFileSync(logPath, "utf8") : "";
  throw new Error(
    `${label} not healthy within ${HEALTH_TIMEOUT_MS / 1000}s at ${url} ` +
      `(last_error=${lastError}).\n${log.slice(-3000)}`,
  );
}

/**
 * The fake IdP's /authorize page: authentik's WebAuthn Authenticator
 * Validation stage, distilled. On load it issues the same request authentik's
 * helper builds — modal (no `mediation`), `allowCredentials: []`, an RP
 * timeout — and mirrors ceremony state into the DOM:
 *   #uvpa            — isUserVerifyingPlatformAuthenticatorAvailable() result
 *   #webauthn-status — data-outcome: "pending" | "resolved" | "rejected:<name>"
 *   #elapsed         — seconds since the ceremony started (visible in footage)
 */
function authorizePageHtml() {
  return `<!doctype html>
<html>
  <head><title>Fake IdP — Authenticator Validation</title></head>
  <body style="font-family: sans-serif; padding: 2rem; max-width: 40rem">
    <h1 style="margin-bottom: 0">Fake IdP (authentik-style)</h1>
    <p>WebAuthn Authenticator Validation stage — sign in with your passkey.</p>
    <div style="border: 1px solid #ccc; border-radius: 8px; padding: 1rem">
      <p>Platform authenticator available (isUVPAA): <b id="uvpa">probing…</b></p>
      <p>RP-supplied timeout: <b>${RP_TIMEOUT_MS} ms</b></p>
      <p>Ceremony state: <b id="webauthn-status">not started</b></p>
      <p>Elapsed since ceremony start: <b id="elapsed">0.0 s</b></p>
    </div>
    <script>
      (function () {
        var statusEl = document.getElementById("webauthn-status");
        var uvpaEl = document.getElementById("uvpa");
        var elapsedEl = document.getElementById("elapsed");
        var start = Date.now();
        setInterval(function () {
          elapsedEl.textContent = ((Date.now() - start) / 1000).toFixed(1) + " s";
        }, 100);
        if (
          window.PublicKeyCredential &&
          PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable
        ) {
          PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().then(
            function (v) {
              uvpaEl.textContent = String(v);
            },
            function (e) {
              uvpaEl.textContent = "error: " + e.name;
            },
          );
        } else {
          uvpaEl.textContent = "unavailable";
        }
        statusEl.dataset.outcome = "pending";
        statusEl.textContent = "pending — waiting for a passkey prompt…";
        var challenge = new Uint8Array(32);
        crypto.getRandomValues(challenge);
        // authentik's stage helper: a normal modal request (no mediation),
        // missing allowCredentials defaulted to [] (discoverable credential).
        navigator.credentials
          .get({
            publicKey: {
              challenge: challenge,
              timeout: ${RP_TIMEOUT_MS},
              allowCredentials: [],
              userVerification: "preferred",
            },
          })
          .then(
            function () {
              statusEl.dataset.outcome = "resolved";
              statusEl.textContent = "resolved — passkey ceremony completed";
            },
            function (e) {
              statusEl.dataset.outcome = "rejected:" + e.name;
              statusEl.textContent = "rejected: " + e.name + " — " + e.message;
            },
          );
      })();
    </script>
  </body>
</html>`;
}

/**
 * Stand up the fake OIDC IdP: discovery + the WebAuthn /authorize page. The
 * issuer is advertised on `localhost` (a valid WebAuthn RP ID and a secure
 * context over plain http), while the omnigent server it authenticates is on
 * `127.0.0.1` — so the IdP is a genuinely third-party origin to the app.
 *
 * @returns {Promise<{ issuer: string, origin: string, close: () => void }>}
 */
async function startFakeIdp() {
  const port = await findFreePort();
  const issuer = `http://localhost:${port}`;
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, issuer);
    if (url.pathname === "/.well-known/openid-configuration") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          issuer,
          authorization_endpoint: `${issuer}/authorize`,
          token_endpoint: `${issuer}/token`,
          jwks_uri: `${issuer}/jwks`,
          userinfo_endpoint: `${issuer}/userinfo`,
          response_types_supported: ["code"],
          subject_types_supported: ["public"],
          id_token_signing_alg_values_supported: ["RS256"],
        }),
      );
      return;
    }
    if (url.pathname === "/authorize") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(authorizePageHtml());
      return;
    }
    if (url.pathname === "/jwks") {
      // Never reached in this journey (the passkey stage never completes),
      // but present so the endpoint set is coherent.
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ keys: [] }));
      return;
    }
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
  });
  await new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return {
    issuer,
    origin: issuer,
    close: () => server.close(),
  };
}

/**
 * Spawn an OIDC-mode `omnigent server` wired to the fake IdP — the JS analog
 * of tests/e2e_ui/auth/_oidc_server.py (env mirrored from it), so this is a
 * stock self-hosted OIDC deployment, no product changes. No mock LLM is
 * spawned: this journey dies at the sign-in page, so no turn is ever driven
 * (OPENAI_BASE_URL points at a loopback port that is never called).
 *
 * @param {string} tmpDir Scratch dir for db/artifacts/agent/logs.
 * @param {string} issuer The fake IdP's issuer URL (must be up already: stock
 *   OIDC mode fetches the discovery document at boot).
 * @returns {Promise<{ serverUrl: string, close: () => Promise<void> }>}
 */
async function spawnOidcServer(tmpDir, issuer) {
  if (!fs.existsSync(path.join(WEB_UI_DIST, "index.html"))) {
    throw new Error(
      `SPA bundle missing at ${WEB_UI_DIST}. Build it first:\n` +
        `  pnpm --filter web install && pnpm --filter web run build`,
    );
  }

  const serverPort = await findFreePort();
  const serverLog = path.join(tmpDir, "server.log");
  const dbPath = path.join(tmpDir, "test.db");
  const artifactDir = path.join(tmpDir, "artifacts");
  const agentYaml = path.join(tmpDir, "hello_world.yaml");
  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(agentYaml, TEST_AGENT_YAML);

  const serverUrl = `http://127.0.0.1:${serverPort}`;
  // Strip ambient runner/host env so nothing leaks into the spawned server —
  // same guard as desktopHarness.spawnServer / the Python recorder lanes.
  const cleanEnv = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith("OMNIGENT_RUNNER_") && !key.startsWith("OMNIGENT_HOST_"),
    ),
  );
  const serverOut = fs.openSync(serverLog, "w");
  const serverProc = spawn(
    PYTHON,
    [
      "-c",
      "from omnigent.cli import main; main()",
      "server",
      "--host",
      "127.0.0.1",
      "--port",
      String(serverPort),
      "--database-uri",
      `sqlite:///${dbPath}`,
      "--artifact-location",
      artifactDir,
      "--agent",
      agentYaml,
    ],
    {
      env: {
        ...cleanEnv,
        PYTHONPATH: SERVER_PYTHONPATH,
        // Stock self-hosted OIDC mode, mirrored from _oidc_server.py.
        OMNIGENT_AUTH_PROVIDER: "oidc",
        OMNIGENT_AUTH_ENABLED: "1",
        OMNIGENT_LOCAL_SINGLE_USER: "",
        OMNIGENT_OIDC_ISSUER: issuer,
        OMNIGENT_OIDC_CLIENT_ID: "e2e-client",
        OMNIGENT_OIDC_CLIENT_SECRET: "e2e-secret",
        OMNIGENT_OIDC_REDIRECT_URI: `${serverUrl}/auth/callback`,
        OMNIGENT_OIDC_COOKIE_SECRET: crypto.randomBytes(32).toString("hex"),
        // Never called: the sign-in journey ends before any turn runs.
        OPENAI_BASE_URL: `http://127.0.0.1:${serverPort}/never-called/v1`,
        OPENAI_API_KEY: "mock-key",
        ANTHROPIC_API_KEY: "",
        OMNIGENT_WEB_UI_DIST: WEB_UI_DIST,
        NO_PROXY: NO_PROXY_LOOPBACK,
        no_proxy: NO_PROXY_LOOPBACK,
      },
      stdio: ["ignore", serverOut, serverOut],
    },
  );
  let serverSpawnError = null;
  serverProc.on("error", (err) => {
    serverSpawnError = err;
  });

  const close = async () => {
    if (serverProc.exitCode === null) serverProc.kill("SIGTERM");
    await sleep(200);
    try {
      fs.closeSync(serverOut);
    } catch {
      /* already closed */
    }
  };

  try {
    await waitForHealthy(`${serverUrl}/health`, "omnigent server (oidc mode)", serverLog);
  } catch (err) {
    await close();
    throw serverSpawnError ?? err;
  }
  return { serverUrl, close };
}

/**
 * Drive the shared journey prefix: launch on the setup page, type the OIDC
 * server's URL, Connect. Returns once the connect click is dispatched.
 *
 * @param {import("playwright").Page} window
 * @param {string} serverUrl
 */
async function connectToServer(window, serverUrl) {
  const urlField = window.locator("#url");
  await urlField.waitFor({ state: "visible", timeout: 15_000 });
  logStep("setup page shown (#url visible)");
  await urlField.fill(serverUrl);
  logStep(`server URL typed: ${serverUrl}`);
  await window.locator("#connect").click({ timeout: 10_000 });
  logStep("connect clicked");
}

/**
 * Close the desktop app with the video finalized and a hard bound on hang.
 * Closing the page's BrowserContext first flushes/finalizes the recording
 * (Playwright only completes the .webm on context close); then the app gets a
 * bounded close — an Electron shell stuck on teardown (e.g. a renderer held
 * by a pending modal WebAuthn request — the very bug under test) would
 * otherwise hang the run forever, so it is force-killed after the grace
 * period rather than allowed to wedge CI.
 *
 * @param {import("playwright").ElectronApplication} electronApp
 * @param {import("playwright").Page} window
 */
async function closeDesktop(electronApp, window) {
  try {
    await Promise.race([window.context().close(), sleep(10_000)]);
  } catch {
    /* context already gone */
  }
  let forced = false;
  await Promise.race([
    electronApp.close().catch(() => {}),
    sleep(10_000).then(() => {
      forced = true;
      try {
        electronApp.process().kill("SIGKILL");
      } catch {
        /* already exited */
      }
    }),
  ]);
  logStep(forced ? "electron close timed out; force-killed" : "electron closed cleanly");
}

describe(
  "desktop shell — self-hosted OIDC sign-in",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let idp;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "oidc-idp-e2e-"));
      // The IdP must be up before the server boots: stock OIDC mode fetches
      // the discovery document at construction time.
      idp = await startFakeIdp();
      logStep(`fake IdP up at ${idp.issuer}`);
      server = await spawnOidcServer(tmpDir, idp.issuer);
      logStep(`oidc-mode omnigent server healthy at ${server.serverUrl}`);
    });

    after(async () => {
      if (server) await server.close();
      if (idp) idp.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    // This guard covers in-window authorization; unit tests cover the positive handoff.
    // RFC 8252 §8.12: "native apps MUST NOT use embedded
    // user-agents to perform authorization requests". Connecting to a
    // self-hosted OIDC server must hand the IdP sign-in off to the system
    // browser (as the Databricks/Okta path already does); the third-party
    // IdP page must never render inside the Electron window.
    it("does not render the third-party IdP sign-in page inside the Electron window", async () => {
      const recordDir = path.join(RECORDINGS_ROOT, "raw-in-window-idp");
      const { electronApp, window, userDataDir } = await launchDesktop({ recordDir });
      let inWindowIdpUrl;
      try {
        logStep("desktop launched; driving setup page");
        await connectToServer(window, server.serverUrl);
        // Bounded observation window: unauthenticated SPA -> /v1/me 401 ->
        // /auth/login -> 302 to the IdP. On the buggy build the MAIN WINDOW
        // lands on the IdP origin within a few seconds; on a fixed build it
        // never does (the sign-in opens externally) and this times out.
        inWindowIdpUrl = await window
          .waitForURL((u) => u.origin === idp.origin, { timeout: 15_000 })
          .then(() => window.url())
          .catch(() => null);
        if (inWindowIdpUrl) {
          const statusEl = window.locator("#webauthn-status");
          await statusEl.waitFor({ state: "visible", timeout: 10_000 });
          const settled = await window
            .waitForFunction(
              () => document.getElementById("webauthn-status")?.dataset.outcome !== "pending",
              { timeout: SETTLE_DEADLINE_MS },
            )
            .then(() => true)
            .catch(() => false);
          const outcome = await statusEl.getAttribute("data-outcome");
          const uvpa = await window
            .locator("#uvpa")
            .textContent()
            .catch(() => "<no probe>");
          const elapsed = await window
            .locator("#elapsed")
            .textContent()
            .catch(() => "?");
          logStep(`main window navigated to IdP: ${inWindowIdpUrl}`);
          logStep(`isUserVerifyingPlatformAuthenticatorAvailable: ${uvpa}`);
          logStep(
            `ceremony ${settled ? "settled" : "remained pending"} after ${elapsed}: ${outcome}`,
          );
        }
        assert.equal(
          inWindowIdpUrl,
          null,
          `third-party IdP sign-in page rendered INSIDE the Electron window ` +
            `(embedded user-agent; RFC 8252 §8.12 violation): ${inWindowIdpUrl}`,
        );
      } finally {
        await closeDesktop(electronApp, window);
        const saved = saveRecording(recordDir, "in-window-idp");
        logStep(`recording(s) saved: ${saved.join(", ") || "<none>"}`);
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    });
  },
);
