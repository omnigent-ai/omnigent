// Discovery and invocation of the local `omnigent` CLI for the desktop shell.
//
// The desktop manages servers by shelling out to the same `omnigent` binary a
// user would run by hand — `server start|stop|status` and `host status` (the
// long-lived `host` connection is spawned by server_manager.js, which owns its
// lifetime). This module locates the binary, runs the short exit-quick
// commands, and parses their `--json` output. The CLE is the single source of
// truth for live state; nothing here is persisted.
//
// Unlike src/url.js this is main-process only (it needs child_process / fs),
// so it's a plain CommonJS module — never loaded in the renderer.
//
// The pure helpers (matchesServer, connectionFromStatus, normalizeServerUrl,
// candidatePaths, resolveCliPath with injected probes) are unit-tested in
// test/omnigent_cli.test.js; the functions that actually spawn a binary are
// exercised in the manual verification flow.

"use strict";

const { execFile, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const url = require("./url");

/** Default timeout for the short status commands. */
const DEFAULT_TIMEOUT_MS = 10000;

/**
 * One-liner shown on the setup page when the CLI is missing. Mirrors the
 * install instructions in the repo root README.
 */
const INSTALL_COMMAND =
  "curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh";

/**
 * Strip a trailing slash so URL comparisons survive the difference between
 * what the user typed and what the CLI records in a daemon target.
 *
 * @param {unknown} value
 * @returns {string}
 */
function normalizeServerUrl(value) {
  if (typeof value !== "string") return "";
  return value.trim().replace(/\/+$/, "");
}

/**
 * True when a server URL points at the local machine — loopback host. Only
 * loopback servers expose the local-server start/stop controls. Reuses the
 * shared LOCAL_HOSTS set from url.js so the desktop never disagrees on what
 * "local" means.
 *
 * @param {string} serverUrl
 * @returns {boolean}
 */
function isLoopbackServer(serverUrl) {
  try {
    return url.LOCAL_HOSTS.has(new URL(serverUrl).hostname);
  } catch {
    return false;
  }
}

/**
 * True when two URLs refer to the same local server — both loopback hosts on
 * the same port (so ``localhost:6767`` and ``127.0.0.1:6767`` match, but
 * ``localhost:8000`` does not). Used to confirm the CLI's local server is the
 * one a window is actually connected to before showing its controls.
 *
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
function sameLoopbackServer(a, b) {
  try {
    const ua = new URL(a);
    const ub = new URL(b);
    if (!url.LOCAL_HOSTS.has(ua.hostname) || !url.LOCAL_HOSTS.has(ub.hostname)) return false;
    return ua.port === ub.port;
  } catch {
    return false;
  }
}

/**
 * Well-known install locations for the `omnigent` binary, in priority order.
 * `uv tool install` (the documented installer) drops it in ~/.local/bin;
 * the rest cover Homebrew and source/cargo installs. Probing these matters
 * because a GUI-launched Electron app inherits a minimal PATH that usually
 * omits ~/.local/bin, so `command -v` alone is not enough.
 *
 * @returns {string[]}
 */
function candidatePaths() {
  const home = os.homedir();
  return [
    path.join(home, ".local", "bin", "omnigent"),
    path.join(home, ".cargo", "bin", "omnigent"),
    "/opt/homebrew/bin/omnigent",
    "/usr/local/bin/omnigent",
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
 * Resolve `omnigent` on PATH (or the user's login shell PATH). Returns null
 * when not found. On POSIX we go through `command -v` so shell-managed PATHs
 * (uv shims) resolve; on Windows we use `where`.
 *
 * @returns {string | null}
 */
function whichOmnigent() {
  try {
    if (process.platform === "win32") {
      const out = execFileSync("where", ["omnigent"], { encoding: "utf8" });
      return out.trim().split(/\r?\n/)[0] || null;
    }
    const out = execFileSync("/bin/sh", ["-c", "command -v omnigent"], {
      encoding: "utf8",
    });
    return out.trim() || null;
  } catch {
    return null;
  }
}

/**
 * Locate the `omnigent` binary. Resolution order: a user-configured path, then
 * PATH, then the well-known candidate locations. Returns the resolved path and
 * which source matched, or null if nothing usable was found.
 *
 * `deps` lets the tests inject the executability/PATH probes so the resolution
 * order can be verified without a real binary on disk.
 *
 * @param {string | null | undefined} configuredPath settings.omnigent_path
 * @param {{
 *   isExecutableFile?: (p: string) => boolean,
 *   whichOmnigent?: () => string | null,
 *   candidatePaths?: () => string[],
 * }} [deps]
 * @returns {{ path: string, source: "configured" | "path" | "candidate" } | null}
 */
function resolveCliPath(configuredPath, deps = {}) {
  const isExec = deps.isExecutableFile || isExecutableFile;
  const which = deps.whichOmnigent || whichOmnigent;
  const candidates = (deps.candidatePaths || candidatePaths)();

  if (configuredPath && isExec(configuredPath)) {
    return { path: configuredPath, source: "configured" };
  }
  const onPath = which();
  if (onPath && isExec(onPath)) {
    return { path: onPath, source: "path" };
  }
  for (const candidate of candidates) {
    if (isExec(candidate)) {
      return { path: candidate, source: "candidate" };
    }
  }
  return null;
}

/**
 * Run an `omnigent` subcommand and resolve with its captured output. Never
 * rejects — a failure surfaces as a non-zero `code` plus stderr so callers can
 * decide. `execFile` (no shell) avoids quoting pitfalls.
 *
 * @param {string} cliPath
 * @param {string[]} args
 * @param {{ timeoutMs?: number }} [opts]
 * @returns {Promise<{ code: number, stdout: string, stderr: string }>}
 */
function runCli(cliPath, args, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    execFile(cliPath, args, { timeout: timeoutMs, encoding: "utf8" }, (err, stdout, stderr) => {
      // execFile sets err.code to the numeric exit code on a normal non-zero
      // exit, or a string errno (e.g. "ENOENT") when the spawn itself failed.
      const code = err ? (typeof err.code === "number" ? err.code : 1) : 0;
      resolve({ code, stdout: stdout || "", stderr: stderr || "" });
    });
  });
}

/**
 * Parse the first JSON object out of CLI stdout. The status commands emit a
 * single JSON blob, but tolerate a stray leading warning line by falling back
 * to the outermost `{…}` slice. Returns null when nothing parses.
 *
 * @param {string} stdout
 * @returns {Record<string, unknown> | null}
 */
function parseJsonLoose(stdout) {
  const text = (stdout || "").trim();
  if (text === "") return null;
  try {
    return JSON.parse(text);
  } catch {
    /* fall through to the slice attempt */
  }
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(text.slice(start, end + 1));
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * Probe the CLI and report whether it's installed and usable. Validates the
 * resolved path by actually running `--version`, so a stale or wrong configured
 * path reports `installed:false` rather than failing later.
 *
 * @param {string | null | undefined} configuredPath
 * @returns {Promise<{
 *   installed: boolean,
 *   path: string | null,
 *   version: string | null,
 *   source: string | null,
 *   installCommand: string,
 * }>}
 */
async function getCliStatus(configuredPath) {
  const resolved = resolveCliPath(configuredPath);
  if (!resolved) {
    return {
      installed: false,
      path: null,
      version: null,
      source: null,
      installCommand: INSTALL_COMMAND,
    };
  }
  const res = await runCli(resolved.path, ["--version"], { timeoutMs: 5000 });
  const ok = res.code === 0;
  return {
    installed: ok,
    path: ok ? resolved.path : null,
    version: ok ? res.stdout.trim() || res.stderr.trim() || null : null,
    source: ok ? resolved.source : null,
    installCommand: INSTALL_COMMAND,
  };
}

/**
 * `omnigent server status --json`. Returns the parsed payload, or a synthetic
 * not-running shape when the command produced no JSON.
 *
 * @param {string} cliPath
 * @returns {Promise<Record<string, unknown>>}
 */
async function getServerStatus(cliPath) {
  const res = await runCli(cliPath, ["server", "status", "--json"]);
  const json = parseJsonLoose(res.stdout);
  if (!json) {
    return { running: false, error: res.stderr.trim() || "could not read server status" };
  }
  return json;
}

/**
 * Start (or reuse) the local background server, then re-read status for a
 * reliable URL. `server start` is idempotent on the CLI side.
 *
 * @param {string} cliPath
 * @returns {Promise<{ ok: boolean, url?: string, port?: number, pid?: number, error?: string }>}
 */
async function startLocalServer(cliPath) {
  const res = await runCli(cliPath, ["server", "start"], { timeoutMs: 30000 });
  const status = await getServerStatus(cliPath);
  if (status && status.running && typeof status.url === "string") {
    return { ok: true, url: status.url, port: status.port, pid: status.pid };
  }
  return {
    ok: false,
    error: res.stderr.trim() || res.stdout.trim() || "failed to start the local server",
  };
}

/**
 * Stop the local background server (and its attached host daemon).
 *
 * @param {string} cliPath
 * @returns {Promise<{ ok: boolean, output: string }>}
 */
async function stopLocalServer(cliPath) {
  const res = await runCli(cliPath, ["server", "stop"], { timeoutMs: 15000 });
  return { ok: res.code === 0, output: (res.stdout || res.stderr).trim() };
}

/**
 * `omnigent host status --json`, optionally scoped to one server. Returns the
 * parsed payload (with a `daemons` array) or null.
 *
 * @param {string} cliPath
 * @param {string | null} [serverUrl]
 * @returns {Promise<Record<string, unknown> | null>}
 */
async function getHostStatus(cliPath, serverUrl) {
  const args = ["host", "status", "--json"];
  if (serverUrl) args.push("--server", serverUrl);
  const res = await runCli(cliPath, args);
  return parseJsonLoose(res.stdout);
}

/**
 * Tell the server to drop a host daemon it owns for this target. Used to
 * disconnect a daemon the desktop adopted rather than spawned.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @returns {Promise<{ ok: boolean, output: string }>}
 */
async function stopHost(cliPath, serverUrl) {
  const res = await runCli(cliPath, ["host", "stop", "--server", serverUrl], {
    timeoutMs: 15000,
  });
  return { ok: res.code === 0, output: (res.stdout || res.stderr).trim() };
}

/**
 * True when a daemon record refers to the given server URL, comparing both its
 * `server_url` and `target` fields after trailing-slash normalization.
 *
 * @param {Record<string, unknown>} daemon One entry from the daemons array.
 * @param {string} serverUrl
 * @returns {boolean}
 */
function matchesServer(daemon, serverUrl) {
  if (!daemon || typeof daemon !== "object") return false;
  const want = normalizeServerUrl(serverUrl);
  if (want === "") return false;
  return (
    normalizeServerUrl(daemon.server_url) === want || normalizeServerUrl(daemon.target) === want
  );
}

/**
 * Reduce a `host status --json` payload to this machine's connection to one
 * server. `connected` requires both a live daemon process and an online host
 * tunnel — the two-field check the CLI itself uses.
 *
 * @param {Record<string, unknown> | null} statusJson
 * @param {string} serverUrl
 * @returns {{
 *   connected: boolean,
 *   process: "online" | "offline",
 *   hostStatus: string | null,
 *   pid: number | null,
 *   error: string | null,
 * }}
 */
function connectionFromStatus(statusJson, serverUrl) {
  const daemons = statusJson && Array.isArray(statusJson.daemons) ? statusJson.daemons : [];
  const daemon = daemons.find((d) => matchesServer(d, serverUrl)) || null;
  if (!daemon) {
    return {
      connected: false,
      process: "offline",
      hostStatus: null,
      pid: null,
      error: null,
    };
  }
  const proc = daemon.process === "online" ? "online" : "offline";
  const hostStatus = typeof daemon.host_status === "string" ? daemon.host_status : null;
  return {
    connected: proc === "online" && hostStatus === "online",
    process: proc,
    hostStatus,
    pid: typeof daemon.pid === "number" ? daemon.pid : null,
    error: typeof daemon.error === "string" ? daemon.error : null,
  };
}

module.exports = {
  INSTALL_COMMAND,
  DEFAULT_TIMEOUT_MS,
  normalizeServerUrl,
  isLoopbackServer,
  sameLoopbackServer,
  candidatePaths,
  isExecutableFile,
  whichOmnigent,
  resolveCliPath,
  runCli,
  parseJsonLoose,
  getCliStatus,
  getServerStatus,
  startLocalServer,
  stopLocalServer,
  getHostStatus,
  stopHost,
  matchesServer,
  connectionFromStatus,
};
