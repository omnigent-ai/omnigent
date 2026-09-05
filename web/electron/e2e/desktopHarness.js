// Recording harness for the Omnigent desktop shell (Electron).
//
// Every OTHER e2e recording lane is a pytest-playwright test under
// tests/e2e_ui/, because the bug lives in the SPA and pytest-playwright's
// `--video` films the browser page. The desktop shell is different: the bug
// lives in the Electron main process (window-open policy, the dead-end 401
// fallback, in-window IdP rendering, session-expiry reload), which a browser
// page never exercises. Python Playwright also has NO Electron API
// (`_electron` is JS-only), so this lane can't ride the Python suite at all.
//
// So this is the desktop analog of tests/e2e_ui/auth/_oidc_server.py: a small
// JS harness that (1) spawns the same mock-LLM + `omnigent server` pair the
// Python suite spawns, and (2) launches the REAL packaged main process via
// Playwright's `_electron.launch({ recordVideo })`, so the recorded video is
// the actual desktop window — the setup page, the connect, and the shell (or
// the failure) a desktop user sees — not a browser tab standing in for it.
//
// Requires `electron` and `playwright` on disk (both are heavy and NOT in the
// web-test CI path); see e2e/README.md. Callers that can't satisfy those skip
// gracefully via `desktopDepsAvailable()`.

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");

/** Repo root: web/electron/e2e → ../../.. */
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
/** The Electron app package root (its package.json `main` is src/main.js). */
const APP_ROOT = path.resolve(__dirname, "..");
/** The SPA the server serves; the Python suite builds it into here too. */
const WEB_UI_DIST = path.join(REPO_ROOT, "omnigent", "server", "static", "web-ui");
/** The mock LLM server the Python suite drives, reused verbatim. */
const MOCK_LLM_SERVER = path.join(
  REPO_ROOT,
  "tests",
  "server",
  "integration",
  "mock_llm_server.py",
);

/** The Python interpreter used to run the server + mock (override for venvs). */
const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

/** A minimal agent spec, mirroring conftest's _TEST_AGENT_YAML. The
 * ``executor.harness`` is required (the spec loader rejects the spec without
 * it), so keep the shape in sync with the Python suite's fixture. */
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

const HEALTH_TIMEOUT_MS = 30_000;
const HEALTH_POLL_MS = 500;

/**
 * Whether the two heavy runtime deps this lane needs are importable. Callers
 * (e.g. the example test) skip when false instead of failing, so a checkout
 * without them stays green.
 *
 * @returns {{ ok: boolean, missing: string[] }}
 */
function desktopDepsAvailable() {
  const missing = [];
  for (const dep of ["playwright", "electron"]) {
    try {
      require.resolve(dep);
    } catch {
      missing.push(dep);
    }
  }
  return { ok: missing.length === 0, missing };
}

/** Sleep `ms` milliseconds. Block body so the Promise executor returns nothing. */
function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/** Resolve a free TCP port by binding to :0 and reading it back. */
function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** GET a URL, resolving the status code (or rejecting on connect error). */
function httpStatus(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      res.resume(); // drain
      resolve(res.statusCode);
    });
    req.on("error", reject);
    req.setTimeout(2000, () => req.destroy(new Error("timeout")));
  });
}

/** Poll `url` until it returns 200 or the deadline passes. */
async function waitForHealthy(url, label, logPath) {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  let lastError = "not polled yet";
  // Health polling is inherently sequential — each probe must await the prior
  // one and the backoff between them; parallelizing defeats the purpose.
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
 * Spawn the mock-LLM server and an `omnigent server` wired to it, mirroring
 * the env + argv of tests/e2e_ui/conftest.py's `mock_llm_server` +
 * `live_server` fixtures (so the desktop shell talks to the same fake backend
 * the Python lanes do — no real provider creds, deterministic replies).
 *
 * The caller MUST have built the SPA into WEB_UI_DIST first (see README): the
 * server serves it from there, and building it lazily under the recorder
 * starves the boot. Throws if the dist dir is missing so the failure is named,
 * not a blank window.
 *
 * @param {string} tmpDir A scratch dir for the db, artifacts, agent, and logs.
 * @returns {Promise<{ serverUrl: string, close: () => Promise<void> }>}
 */
async function spawnServer(tmpDir) {
  if (!fs.existsSync(path.join(WEB_UI_DIST, "index.html"))) {
    throw new Error(
      `SPA bundle missing at ${WEB_UI_DIST}. Build it first:\n` +
        `  pnpm --filter web install && pnpm --filter web run build`,
    );
  }

  const mockPort = await findFreePort();
  const serverPort = await findFreePort();
  const mockLog = path.join(tmpDir, "mock_llm.log");
  const serverLog = path.join(tmpDir, "server.log");
  const dbPath = path.join(tmpDir, "test.db");
  const artifactDir = path.join(tmpDir, "artifacts");
  const agentYaml = path.join(tmpDir, "hello_world.yaml");
  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(agentYaml, TEST_AGENT_YAML);

  const mockOut = fs.openSync(mockLog, "w");
  const mockProc = spawn(PYTHON, [MOCK_LLM_SERVER, String(mockPort)], {
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
    stdio: ["ignore", mockOut, mockOut],
  });
  // A bad PYTHON (ENOENT) fires 'error' async; surface it as a rejection rather
  // than an uncaught event that crashes the runner. Record it so the health
  // wait can report it instead of a bare timeout.
  let mockSpawnError = null;
  mockProc.on("error", (err) => {
    mockSpawnError = err;
  });
  const mockUrl = `http://127.0.0.1:${mockPort}`;
  try {
    await waitForHealthy(`${mockUrl}/stats`, "mock LLM server", mockLog);
  } catch (err) {
    if (mockProc.exitCode === null) mockProc.kill("SIGTERM");
    try {
      fs.closeSync(mockOut);
    } catch {
      /* already closed */
    }
    throw mockSpawnError ?? err;
  }

  const serverOut = fs.openSync(serverLog, "w");
  // Strip ambient runner/host env so a nested runner (if the journey starts a
  // host) boots clean rather than taking the zygote-fork path and hanging —
  // the same leak the Python recorder guards against. Rebuild by filtering
  // (rather than deleting keys) to keep the object shape static.
  const cleanEnv = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith("OMNIGENT_RUNNER_") && !key.startsWith("OMNIGENT_HOST_"),
    ),
  );
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
        PYTHONPATH: REPO_ROOT,
        OPENAI_BASE_URL: `${mockUrl}/v1`,
        OPENAI_API_KEY: "mock-key",
        ANTHROPIC_API_KEY: "",
        OMNIGENT_WEB_UI_DIST: WEB_UI_DIST,
      },
      stdio: ["ignore", serverOut, serverOut],
    },
  );
  let serverSpawnError = null;
  serverProc.on("error", (err) => {
    serverSpawnError = err;
  });
  const serverUrl = `http://127.0.0.1:${serverPort}`;

  const close = async () => {
    for (const proc of [serverProc, mockProc]) {
      if (proc.exitCode === null) {
        proc.kill("SIGTERM");
      }
    }
    // Give them a moment; escalate is left to process teardown.
    await sleep(200);
    try {
      fs.closeSync(serverOut);
    } catch {
      /* already closed */
    }
    try {
      fs.closeSync(mockOut);
    } catch {
      /* already closed */
    }
  };

  try {
    await waitForHealthy(`${serverUrl}/health`, "omnigent server", serverLog);
  } catch (err) {
    await close();
    throw serverSpawnError ?? err;
  }
  return { serverUrl, close };
}

/**
 * Launch the real desktop shell (Electron main process) under Playwright with
 * video recording on, in an isolated userData dir so it never touches the
 * developer's real settings. Optionally pre-seed a saved `server_url` so the
 * app boots straight onto the server (skipping the setup page) — for a bug
 * whose failure is PAST connect. Omit it to film the connect journey itself.
 *
 * @param {object} opts
 * @param {string} opts.recordDir Directory the .webm video is written into.
 * @param {string} [opts.serverUrl] Pre-seed settings.json's server_url with
 *   this, so the shell auto-connects on launch.
 * @param {string} [opts.userDataDir] Override the isolated userData dir
 *   (defaults to a fresh temp dir).
 * @returns {Promise<{ electronApp: import("playwright").ElectronApplication,
 *   window: import("playwright").Page, userDataDir: string }>}
 */
async function launchDesktop(opts) {
  const { _electron: electron } = require("playwright");
  const userDataDir = opts.userDataDir || fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-"));
  fs.mkdirSync(userDataDir, { recursive: true });
  if (opts.serverUrl) {
    fs.writeFileSync(
      path.join(userDataDir, "settings.json"),
      JSON.stringify({ server_url: opts.serverUrl }, null, 2),
    );
  }
  fs.mkdirSync(opts.recordDir, { recursive: true });

  const args = [APP_ROOT, `--user-data-dir=${userDataDir}`];
  // Headless-Linux / CI hardening, gated on the same env var the Python e2e_ui
  // suite uses (conftest.browser_type_launch_args). Under xvfb — and especially
  // as root or in a container — Electron's Chromium refuses to start without
  // --no-sandbox, and --disable-dev-shm-usage avoids the tiny /dev/shm a
  // container gives it. Off by default so local (macOS/dev) runs are unchanged.
  if (process.env.OMNIGENT_PW_NO_SANDBOX) {
    args.push("--no-sandbox", "--disable-dev-shm-usage");
  }

  const electronApp = await electron.launch({
    args,
    recordVideo: { dir: opts.recordDir },
    // Dev builds read dev-app-update.yml and would try to reach the update
    // endpoint; a version override keeps the app off the update path.
    env: { ...process.env, OMNIGENT_DESKTOP_VERSION_OVERRIDE: "999.0.0" },
  });
  // If firstWindow() throws after launch() succeeded, close the app here so the
  // Electron process isn't orphaned (the caller never got a handle to close).
  let window;
  try {
    window = await electronApp.firstWindow();
  } catch (err) {
    await electronApp.close().catch(() => {});
    throw err;
  }
  return { electronApp, window, userDataDir };
}

/**
 * After the Electron app has closed (which flushes the video), rename the
 * recorded clip(s) to stable names at `recordDir`'s root. Playwright writes one
 * `page@<hash>.webm` per page context (the shell window, plus any OAuth popup /
 * in-window IdP `WebContentsView`). ALL clips are kept — the subject of a
 * popup/IdP bug is the popup, which is often shorter/smaller than the main
 * window, so we must not delete by size. Only the raw `page@<hash>.webm` names
 * (the ones the CURRENT launch produced) are renamed; already-named clips from
 * a prior `saveRecording` are left alone, so calling twice is safe.
 *
 * The largest raw clip (usually the main window) becomes `<name>.webm`; any
 * additional raw clips become `<name>-2.webm`, `<name>-3.webm`, … in
 * descending-size order. When multiple clips exist, inspect each and point the
 * handoff at the one that shows the failure (see e2e/README.md).
 *
 * Call this AFTER `electronApp.close()`.
 *
 * @param {string} recordDir The dir passed to `launchDesktop`.
 * @param {string} name Stable base name, e.g. `"before-connect"` (no suffix).
 * @returns {string[]} Absolute paths of the saved `.webm`(s), largest first;
 *   empty when no video was produced.
 */
function saveRecording(recordDir, name) {
  if (!fs.existsSync(recordDir)) return [];
  // Only the raw per-context files this launch wrote; leave anything already
  // renamed (from a prior call) untouched so repeat calls don't clobber.
  const raw = fs
    .readdirSync(recordDir)
    .filter((f) => f.startsWith("page@") && f.endsWith(".webm"))
    .map((f) => path.join(recordDir, f))
    .filter((p) => fs.statSync(p).isFile());
  if (raw.length === 0) return [];
  // Largest first so the primary (usually the main window) takes the bare name.
  raw.sort((a, b) => fs.statSync(b).size - fs.statSync(a).size);
  const saved = [];
  raw.forEach((clip, i) => {
    const dest = path.join(recordDir, i === 0 ? `${name}.webm` : `${name}-${i + 1}.webm`);
    fs.renameSync(clip, dest);
    saved.push(dest);
  });
  return saved;
}

module.exports = {
  APP_ROOT,
  REPO_ROOT,
  WEB_UI_DIST,
  desktopDepsAvailable,
  findFreePort,
  spawnServer,
  launchDesktop,
  saveRecording,
};
