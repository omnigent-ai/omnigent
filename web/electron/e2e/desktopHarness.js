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
  const mockUrl = `http://127.0.0.1:${mockPort}`;
  await waitForHealthy(`${mockUrl}/stats`, "mock LLM server", mockLog);

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
    throw err;
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

  const electronApp = await electron.launch({
    args: [APP_ROOT, `--user-data-dir=${userDataDir}`],
    recordVideo: { dir: opts.recordDir },
    // Dev builds read dev-app-update.yml and would try to reach the update
    // endpoint; a version override keeps the app off the update path.
    env: { ...process.env, OMNIGENT_DESKTOP_VERSION_OVERRIDE: "999.0.0" },
  });
  const window = await electronApp.firstWindow();
  return { electronApp, window, userDataDir };
}

/**
 * After the Electron app has closed (which flushes the video), rename the
 * recorded clip to a stable name at `recordDir`'s root. Playwright writes one
 * `page@<hash>.webm` per page context under `recordDir`; the shell window is
 * the largest, so pick that and move it to `<name>.webm` (dropping the rest, so
 * the same footage isn't collected twice). Returns the final path, or null when
 * no video was produced.
 *
 * Call this AFTER `electronApp.close()`.
 *
 * @param {string} recordDir The dir passed to `launchDesktop`.
 * @param {string} name Stable base name, e.g. `"before-connect"` (no suffix).
 * @returns {string | null} Absolute path to the renamed `.webm`, or null.
 */
function saveRecording(recordDir, name) {
  if (!fs.existsSync(recordDir)) return null;
  const clips = fs
    .readdirSync(recordDir)
    .filter((f) => f.endsWith(".webm"))
    .map((f) => path.join(recordDir, f))
    .filter((p) => fs.statSync(p).isFile());
  if (clips.length === 0) return null;
  // The shell window's video is the largest; incidental contexts are tiny.
  clips.sort((a, b) => fs.statSync(b).size - fs.statSync(a).size);
  const [primary, ...rest] = clips;
  const dest = path.join(recordDir, `${name}.webm`);
  fs.renameSync(primary, dest);
  for (const leftover of rest) fs.rmSync(leftover, { force: true });
  return dest;
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
