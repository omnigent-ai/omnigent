"use strict";

/**
 * Arca host connection (Databricks-internal).
 *
 * Arca is Databricks' internal sandbox CLI: each user has one EC2 dev
 * instance, `arca ssh <args...>` passes the args through to ssh against it
 * (starting the instance first when needed). Connecting that instance as an
 * Omnigent host means running, over `arca ssh`:
 *
 *   isaac omni host --server <url> --background --non-interactive
 *
 * (`isaac` is the Databricks-internal launcher that provides the `omni` CLI
 * on Arca instances.)
 *
 * The remote daemon then opens the ordinary outbound host tunnel using the
 * Arca box's own Databricks credentials (synced by arca), so no secret ever
 * leaves this machine. `--background` exits 0 only once the daemon survived
 * startup, and `--non-interactive` fails loud instead of dangling on a browser
 * login — both are what make the exit code a trustworthy signal here.
 *
 * This module is main-process-free: the binary probe and process spawn are
 * injected so everything is unit-testable without Electron or a real arca.
 */

const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

/**
 * Connecting may cold-start the EC2 instance, which takes minutes — give the
 * whole ssh + remote daemon startup a generous ceiling.
 */
const CONNECT_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * The only characters allowed in the server URL that rides inside the ssh
 * remote command. ssh joins argv with spaces and the REMOTE shell re-parses
 * the line, so the URL (the one non-literal argument) must not smuggle shell
 * metacharacters (`;`, `$`, backticks, quotes, spaces…) through its path or
 * query — URL-legal but shell-hostile. Allowlist, not escape: a URL outside
 * this set is refused outright.
 */
const SAFE_URL_RE = /^[A-Za-z0-9\-._~:/?=&%]+$/;

/**
 * Well-known install locations for the arca binary. Probed because a
 * GUI-launched Electron app inherits a minimal PATH (mirrors the omnigent CLI
 * resolution in omnigent_cli.js).
 *
 * @returns {string[]}
 */
function candidatePaths() {
  const home = os.homedir();
  return [
    "/usr/local/bin/arca",
    "/opt/homebrew/bin/arca",
    path.join(home, ".local", "bin", "arca"),
  ];
}

/**
 * True when `p` exists, is a regular file, and is executable by this process.
 *
 * @param {string} p
 * @returns {boolean}
 */
function isExecutableFile(p) {
  try {
    if (!fs.statSync(p).isFile()) return false;
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolve `arca` on PATH via the shell (so login-shell PATHs resolve), else
 * null. Arca is macOS-only, so no Windows branch.
 *
 * @returns {string | null}
 */
function whichArca() {
  try {
    const out = execFileSync("/bin/sh", ["-c", "command -v arca"], { encoding: "utf8" });
    return out.trim() || null;
  } catch {
    return null;
  }
}

/**
 * Locate the arca binary: PATH first, then well-known locations. Null when
 * arca isn't installed on this machine.
 *
 * @param {{
 *   isExecutableFile?: (p: string) => boolean,
 *   whichArca?: () => string | null,
 *   candidatePaths?: () => string[],
 * }} [deps]
 * @returns {string | null}
 */
function resolveArcaPath(deps = {}) {
  const isExec = deps.isExecutableFile || isExecutableFile;
  const onPath = (deps.whichArca || whichArca)();
  if (onPath && isExec(onPath)) return onPath;
  for (const candidate of (deps.candidatePaths || candidatePaths)()) {
    if (isExec(candidate)) return candidate;
  }
  return null;
}

/**
 * Build the arca argv that connects the instance to `serverUrl`. Everything
 * after "ssh" is passed through to ssh and runs as the remote command.
 *
 * @param {string} serverUrl
 * @returns {string[]}
 */
function buildConnectArgs(serverUrl) {
  const url = new URL(serverUrl);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error(`unsupported server URL scheme: ${url.protocol}`);
  }
  if (!SAFE_URL_RE.test(url.toString())) {
    throw new Error("server URL contains characters that are not allowed in an ssh command");
  }
  return [
    "ssh",
    // Ordinary arca ssh inherits -R 19222 from ~/.ssh/config. Arca Companion
    // concurrently reclaims that reserved listener with `fuser -k`, which can
    // kill this session during cold start even after the remote command
    // succeeded. This headless launch needs no forwards, so opt out entirely.
    "-o",
    "ClearAllForwardings=yes",
    "isaac",
    "omni",
    "host",
    "--server",
    url.toString(),
    "--background",
    "--non-interactive",
  ];
}

/**
 * Map a failed connect run to an actionable user-facing result. Matched
 * against known arca / omnigent CLI failure shapes; anything unrecognized
 * falls through to the captured output.
 *
 * @param {{ code: number | null, stdout: string, stderr: string, timedOut?: boolean }} run
 * @returns {{ ok: false, error: string, authError?: boolean }}
 */
function describeConnectFailure(run) {
  const output = `${run.stderr}\n${run.stdout}`;
  if (run.timedOut) {
    return {
      ok: false,
      error:
        "Connecting to Arca timed out. The instance may still be starting — " +
        "check `arca status` and try again.",
    };
  }
  // `omni host --non-interactive` fails loud with a sign-in hint when the
  // Arca box's Databricks credentials can't mint a server token.
  if (/not signed in/i.test(output)) {
    return {
      ok: false,
      authError: true,
      error:
        "The Arca instance isn't signed in to this server. Run " +
        "`arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
    };
  }
  // The remote shell couldn't find isaac (or isaac couldn't find omni) on the
  // Arca instance.
  if (run.code === 127 || /(isaac|omni(gent)?):? .*(command )?not found/i.test(output)) {
    return {
      ok: false,
      error:
        "`isaac omni` isn't available on the Arca instance. " +
        "Check the isaac setup there (`arca ssh`, then `isaac omni --help`) and try again.",
    };
  }
  if (/error connecting to arca/i.test(output)) {
    return {
      ok: false,
      error:
        "Couldn't reach the Arca instance. Try `arca stop && arca start` in a terminal, " +
        "then connect again.",
    };
  }
  const detail = run.stderr.trim() || run.stdout.trim();
  return {
    ok: false,
    error: detail
      ? `Connecting to Arca failed: ${lastLine(detail)}`
      : `Connecting to Arca failed (exit code ${run.code ?? "unknown"}).`,
  };
}

/**
 * The last non-empty line of captured output — arca and ssh are chatty, and
 * the final line is where both put the actual error.
 *
 * @param {string} text
 * @returns {string}
 */
function lastLine(text) {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return lines[lines.length - 1] ?? text;
}

/**
 * Start connecting the user's Arca instance to `serverUrl` as an Omnigent
 * host, streaming the command's live output. Built for the connect console:
 * the caller shows `command` to the user, pipes `onOutput` chunks into a
 * terminal pane, and may `cancel()` (window closed). The promise never
 * rejects — every failure resolves as `{ ok: false, error }`.
 *
 * @param {string} serverUrl The window's connected server URL.
 * @param {{
 *   timeoutMs?: number,
 *   resolveArcaPath?: () => string | null,
 *   spawn?: typeof spawn,
 *   onOutput?: (text: string) => void,
 * }} [deps]
 * @returns {{
 *   command: string | null,
 *   promise: Promise<{
 *     ok: boolean,
 *     alreadyRunning?: boolean,
 *     error?: string,
 *     authError?: boolean,
 *     canceled?: boolean,
 *   }>,
 *   cancel: () => void,
 * }}
 */
function startArcaConnect(serverUrl, deps = {}) {
  const timeoutMs = deps.timeoutMs ?? CONNECT_TIMEOUT_MS;
  const onOutput = deps.onOutput || (() => {});
  const arcaPath = (deps.resolveArcaPath || resolveArcaPath)();
  if (!arcaPath) {
    return {
      command: null,
      promise: Promise.resolve({
        ok: false,
        error: "The arca CLI was not found on this machine.",
      }),
      cancel: () => {},
    };
  }
  let args;
  try {
    args = buildConnectArgs(serverUrl);
  } catch (error) {
    return {
      command: null,
      promise: Promise.resolve({ ok: false, error: `Invalid server URL: ${error.message}` }),
      cancel: () => {},
    };
  }
  const spawnFn = deps.spawn || spawn;
  const child = spawnFn(arcaPath, args, { stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  let timedOut = false;
  let canceled = false;
  const promise = new Promise((resolve) => {
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill();
      } catch {
        // Already gone.
      }
    }, timeoutMs);
    if (typeof timer.unref === "function") timer.unref();
    child.stdout?.on("data", (chunk) => {
      const text = String(chunk);
      stdout += text;
      onOutput(text);
    });
    child.stderr?.on("data", (chunk) => {
      const text = String(chunk);
      stderr += text;
      onOutput(text);
    });
    let settled = false;
    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    child.on("error", (error) => {
      settle({ ok: false, error: `Couldn't run arca: ${error.message}` });
    });
    child.on("exit", (code) => {
      if (canceled) {
        settle({ ok: false, canceled: true, error: "Connecting to Arca was canceled." });
        return;
      }
      if (code === 0) {
        // `omni host --background` reuses a healthy daemon and says so — the
        // caller can then skip waiting for a host that was online all along.
        settle({ ok: true, alreadyRunning: /already running/i.test(stdout + stderr) });
        return;
      }
      settle(describeConnectFailure({ code, stdout, stderr, timedOut }));
    });
  });
  return {
    command: `arca ${args.join(" ")}`,
    promise,
    cancel: () => {
      canceled = true;
      try {
        child.kill();
      } catch {
        // Already gone.
      }
    },
  };
}

/**
 * Connect the user's Arca instance to `serverUrl` as an Omnigent host. Thin
 * non-streaming wrapper over {@link startArcaConnect}; never rejects.
 *
 * @param {string} serverUrl The window's connected server URL.
 * @param {Parameters<typeof startArcaConnect>[1]} [deps]
 * @returns {Promise<{ ok: boolean, error?: string, authError?: boolean }>}
 */
function connectArcaHost(serverUrl, deps = {}) {
  return startArcaConnect(serverUrl, deps).promise;
}

module.exports = {
  CONNECT_TIMEOUT_MS,
  buildConnectArgs,
  connectArcaHost,
  describeConnectFailure,
  resolveArcaPath,
  startArcaConnect,
};
