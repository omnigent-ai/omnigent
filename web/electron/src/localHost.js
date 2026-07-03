// Spawn and supervise a local Omnigent host daemon on the user's machine.
//
// Background: a "host" is a machine that runs agent sessions; it dials OUT to
// the Omnigent server over a websocket tunnel. Normally the user starts one by
// typing `omnigent host` in a terminal. The desktop shell can do it for them —
// it has a real process to fork — so the New Chat empty state can offer a
// one-click "Run on this Mac".
//
// This module mirrors the Python CLI's own background-daemon machinery so the
// two stay interchangeable (a daemon this module spawns is visible to
// `omnigent host status`, and vice-versa):
//   * argv          → `python -m omnigent.host._daemon_entry --local|--server`
//                      (exactly `_ensure_host_daemon` in omnigent/cli.py)
//   * detach        → { detached: true } + child.unref()  (≈ start_new_session)
//   * logs          → ~/.omnigent/logs/host-daemon/daemon-*.log
//   * running-check → ~/.omnigent/daemons/<sha256(target)[:16]>.json + kill(pid,0)
//
// THE HARD PART is locating the runtime. A macOS .app launched from Finder/Dock
// inherits a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so it will NOT see
// ~/.local/bin, /opt/homebrew/bin, or a uv-installed `omni`. We search known
// install dirs first, then fall back to the user's login-shell PATH, and we
// prepend the discovered bin dir to the daemon's PATH so the host's own runners
// can find node/claude/codex.
//
// Pure logic (mode decision, argv, needsLogin, runtime resolution with injected
// fs/exec) is exported for unit tests that import this without `electron`.

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { execFileSync, spawn } = require("node:child_process");
const { LOCAL_HOSTS } = require("./url");

/** Shared per-user Omnigent state dir. Mirrors the CLI's `~/.omnigent`. */
const STATE_DIR = path.join(os.homedir(), ".omnigent");
/** Where the CLI records background daemons (one JSON per target). */
const DAEMON_DIR = path.join(STATE_DIR, "daemons");
/** Where the CLI writes daemon logs; we write ours alongside. */
const DAEMON_LOG_DIR = path.join(STATE_DIR, "logs", "host-daemon");
/** Per-server auth records written by `omnigent login`. */
const AUTH_TOKENS_PATH = path.join(STATE_DIR, "auth_tokens.json");
/** The CLI's marker target for a local (server-owning) daemon. */
const LOCAL_DAEMON_MARKER = "local";

/**
 * Bin directories an Omnigent CLI install commonly lands in but a
 * Finder-launched .app's PATH omits. Order = preference.
 */
function knownBinDirs() {
  const home = os.homedir();
  return [
    path.join(home, ".local", "bin"), // uv tool / pipx default
    "/opt/homebrew/bin", // Homebrew on Apple silicon
    "/usr/local/bin", // Homebrew on Intel / manual installs
    path.join(home, ".local", "share", "uv", "tools", "omnigent", "bin"),
  ];
}

/** TTL for the cached runtime resolution; a retry after install busts it. */
const RUNTIME_CACHE_TTL_MS = 30_000;
let _runtimeCache = null; // { at: epochMs, value: RuntimeInfo | null }

/**
 * Whether a server URL points at this machine's loopback interface. A loopback
 * server needs no login (the token check is skipped for it), but — unlike an
 * absent URL — the host must still connect to that *specific* server, not spawn
 * a fresh one. Empty/unparseable counts as non-loopback here (the caller only
 * uses this to suppress the login hint).
 *
 * @param {string|null} serverUrl
 * @returns {boolean}
 */
function isLoopbackServer(serverUrl) {
  if (!serverUrl) return false;
  try {
    return LOCAL_HOSTS.has(new URL(serverUrl).hostname);
  } catch {
    return false;
  }
}

/**
 * Decide the daemon connection mode for a connected server origin.
 *
 * "local" means `omni host ""` — the daemon SPAWNS AND OWNS a fresh local
 * server. That is only right when there is no server to connect to yet (a
 * from-scratch launch). Whenever the UI is already pinned to a concrete server
 * — even a loopback one it just started — the host must dial THAT server with
 * `--server <url>` ("remote"), or it would spawn a second, competing server the
 * pinned UI has no tunnel to. So only a null/empty/unparseable URL is "local".
 *
 * @param {string|null} serverUrl Connected server origin, or null.
 * @returns {"local"|"remote"}
 */
function decideMode(serverUrl) {
  if (!serverUrl) return "local";
  try {
    // Parse only to validate; an unparseable URL falls back to local spawn.
    return new URL(serverUrl).protocol ? "remote" : "local";
  } catch {
    return "local";
  }
}

/**
 * Build the `omni host` CLI args for a mode/server.
 *
 * `omni host` registers this machine as a host in the foreground (it self-
 * registers in the daemon registry that {@link readDaemonRecord} reads, so a
 * daemon we spawn is visible to `omnigent host status` and we never double-
 * spawn). Local mode uses the empty-string positional `omni host ""`, which
 * the host group's parser binds to `--server` and treats as "spawn + connect
 * to a local server" (see `_HostGroup` in omnigent/cli.py). Remote mode passes
 * `--server <url>`.
 *
 * NOTE: deliberately NOT `python -m omnigent.host._daemon_entry` — that entry
 * does not write the registry record our status check relies on.
 *
 * @param {"local"|"remote"} mode
 * @param {string|null} serverUrl Required (and used) only for "remote".
 * @returns {string[]} e.g. ["host", ""] or ["host", "--server", "https://…"].
 */
function daemonArgs(mode, serverUrl) {
  return mode === "local" ? ["host", ""] : ["host", "--server", serverUrl];
}

/**
 * The CLI's normalized "target" for a mode/server — the key its daemon
 * registry is hashed by. Lets us find a daemon the CLI itself spawned.
 *
 * @param {"local"|"remote"} mode
 * @param {string|null} serverUrl
 * @returns {string} "local" or the trailing-slash-stripped server URL.
 */
function daemonTarget(mode, serverUrl) {
  if (mode === "local" || !serverUrl) return LOCAL_DAEMON_MARKER;
  return serverUrl.replace(/\/+$/, "");
}

/**
 * Path of the CLI daemon-registry JSON for a target:
 * ~/.omnigent/daemons/<sha256(target)[:16]>.json. Mirrors
 * `_daemon_record_path` in omnigent/cli.py.
 *
 * @param {string} target
 * @returns {string}
 */
function daemonRecordPath(target) {
  const digest = crypto.createHash("sha256").update(target, "utf8").digest("hex").slice(0, 16);
  return path.join(DAEMON_DIR, `${digest}.json`);
}

/** True when a process with `pid` exists (kill -0). False on ESRCH/garbage. */
function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM means it exists but we can't signal it — still "alive".
    return err && err.code === "EPERM";
  }
}

/**
 * Read the CLI daemon record for a target and report whether it's running.
 *
 * Reading the CLI's own source of truth (not an Electron-private pid) means a
 * daemon started from a terminal is also detected, so we never double-spawn.
 *
 * @param {string} target
 * @param {{ readFileSync?: typeof fs.readFileSync }} [deps]
 * @returns {{ running: boolean, pid: number|null, logPath: string|null }}
 */
function readDaemonRecord(target, deps = {}) {
  const readFileSync = deps.readFileSync ?? fs.readFileSync;
  let raw;
  try {
    raw = JSON.parse(readFileSync(daemonRecordPath(target), "utf8"));
  } catch {
    return { running: false, pid: null, logPath: null };
  }
  const pid = typeof raw.pid === "number" ? raw.pid : Number.parseInt(raw.pid, 10);
  const logPath = typeof raw.log_path === "string" && raw.log_path ? raw.log_path : null;
  return { running: pidAlive(pid), pid: Number.isInteger(pid) ? pid : null, logPath };
}

/**
 * Whether a remote server lacks a usable stored login token. SOFT signal:
 * ambient credentials (e.g. Databricks) may still authenticate, so callers
 * warn but don't block. Reads the same `~/.omnigent/auth_tokens.json` the
 * `omnigent login` flow writes; the key is the trailing-slash-stripped URL.
 *
 * A Databricks pointer record (auth_type: "databricks", no token) counts as
 * "has credentials" — it points at a CLI OAuth cache the host can mint from.
 *
 * @param {string|null} serverUrl
 * @param {{ readFileSync?: typeof fs.readFileSync, now?: () => number }} [deps]
 * @returns {boolean} True only for a remote server with no usable record.
 */
function computeNeedsLogin(serverUrl, deps = {}) {
  // A local/loopback server needs no login, and an absent URL (local spawn
  // mode) needs none either.
  if (!serverUrl || isLoopbackServer(serverUrl)) return false;
  const readFileSync = deps.readFileSync ?? fs.readFileSync;
  const now = deps.now ?? Date.now;
  let data;
  try {
    data = JSON.parse(readFileSync(AUTH_TOKENS_PATH, "utf8"));
  } catch {
    return true; // no token file → no stored credentials
  }
  const entry = data[serverUrl.replace(/\/+$/, "")];
  if (!entry || typeof entry !== "object") return true;
  if (entry.auth_type === "databricks") return false; // pointer record → ambient OAuth
  if (typeof entry.token !== "string" || entry.token === "") return true;
  // Expired tokens count as missing (mirrors cli_auth.load_token).
  if (typeof entry.expires_at === "number" && entry.expires_at * 1000 < now()) return true;
  return false;
}

/**
 * Locate the Omnigent runtime, preferring an `omnigent`/`omni` binary. Tries,
 * in order: known install dirs, the login shell's PATH (the Finder minimal-PATH
 * escape hatch), then the bare process PATH. Caches the first hit with a short
 * TTL so a post-install retry re-probes.
 *
 * @param {{
 *   accessSync?: typeof fs.accessSync,
 *   runShellProbe?: () => string|null,
 *   pathEnv?: string,
 *   binDirs?: string[],
 *   now?: () => number,
 *   noCache?: boolean,
 * }} [deps]
 * @returns {{ binary: string, binDir: string }|null} Resolved runtime, or null.
 */
function resolveOmnigentRuntime(deps = {}) {
  const now = deps.now ?? Date.now;
  if (!deps.noCache && _runtimeCache && now() - _runtimeCache.at < RUNTIME_CACHE_TTL_MS) {
    return _runtimeCache.value;
  }
  const accessSync = deps.accessSync ?? fs.accessSync;
  const binDirs = deps.binDirs ?? knownBinDirs();
  const names = ["omnigent", "omni"];

  const isExec = (p) => {
    try {
      accessSync(p, fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  };

  let resolved = null;
  // 1) Known install dirs.
  outer: for (const dir of binDirs) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      if (isExec(candidate)) {
        resolved = { binary: candidate, binDir: dir };
        break outer;
      }
    }
  }
  // 2) Login-shell PATH probe (Finder minimal-PATH escape hatch).
  if (!resolved) {
    const probe = (deps.runShellProbe ?? defaultShellProbe)();
    if (probe && isExec(probe)) resolved = { binary: probe, binDir: path.dirname(probe) };
  }
  // 3) Bare process PATH (covers `npm run dev` / a normal-PATH launch).
  if (!resolved) {
    const pathEnv = deps.pathEnv ?? process.env.PATH ?? "";
    outer2: for (const dir of pathEnv.split(path.delimiter)) {
      if (!dir) continue;
      for (const name of names) {
        const candidate = path.join(dir, name);
        if (isExec(candidate)) {
          resolved = { binary: candidate, binDir: dir };
          break outer2;
        }
      }
    }
  }

  if (!deps.noCache) _runtimeCache = { at: now(), value: resolved };
  return resolved;
}

/** Clear the runtime resolution cache (used by the "retry after install" path). */
function clearRuntimeCache() {
  _runtimeCache = null;
}

/**
 * Ask the user's login shell where `omnigent`/`omni` resolves. Time-bounded and
 * never throws — a slow or broken shell must not stall the connect flow.
 *
 * @returns {string|null} Absolute path to the resolved binary, or null.
 */
function defaultShellProbe() {
  const shell = process.env.SHELL || "/bin/zsh";
  try {
    const out = execFileSync(shell, ["-lic", "command -v omnigent || command -v omni"], {
      timeout: 4000,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    });
    const line = out
      .split("\n")
      .map((s) => s.trim())
      .find(Boolean);
    return line && line.startsWith("/") ? line : null;
  } catch {
    return null;
  }
}

/**
 * Report the local-host state for a connected server. Pure-ish: all filesystem
 * and runtime probing flows through injectable deps so this is unit-testable.
 *
 * @param {{ serverUrl: string|null }} params
 * @param {object} [deps] Injected fs/probe/now for tests.
 * @returns {import("./localHost").LocalHostStatus}
 */
function getLocalHostStatus({ serverUrl }, deps = {}) {
  const mode = decideMode(serverUrl);
  const target = daemonTarget(mode, serverUrl);
  const record = readDaemonRecord(target, deps);
  const runtime = resolveOmnigentRuntime(deps);
  return {
    running: record.running,
    runtimeAvailable: runtime !== null,
    mode,
    needsLogin: computeNeedsLogin(serverUrl, deps),
    serverUrl: serverUrl ?? null,
    logPath: record.logPath,
  };
}

/**
 * Spawn a detached host daemon for the connected server, unless one is already
 * running for that target. Returns a structured result (never throws) so the
 * renderer can show an error and fall back to the CLI instructions.
 *
 * @param {{ serverUrl: string|null }} params
 * @returns {Promise<import("./localHost").StartLocalHostResult>}
 */
async function startLocalHost({ serverUrl }) {
  const mode = decideMode(serverUrl);
  const target = daemonTarget(mode, serverUrl);

  const existing = readDaemonRecord(target);
  if (existing.running) {
    return { ok: true, logPath: existing.logPath }; // already up — never double-spawn
  }

  const runtime = resolveOmnigentRuntime();
  if (!runtime) {
    return {
      ok: false,
      error: "Couldn't find the Omnigent CLI on this machine. Install it, then retry.",
    };
  }

  let logFd = null;
  let logPath = null;
  try {
    fs.mkdirSync(DAEMON_LOG_DIR, { recursive: true });
    logPath = path.join(
      DAEMON_LOG_DIR,
      `daemon-electron-${process.pid}-${target.replace(/[^\w.-]/g, "_")}.log`,
    );
    logFd = fs.openSync(logPath, "a");
  } catch (err) {
    return { ok: false, error: `Couldn't open a log file: ${err.message}` };
  }

  // Prepend the runtime's bin dir to PATH so the daemon AND the runners it
  // spawns can find node/claude/codex despite the app's minimal PATH.
  const childPath = `${runtime.binDir}${path.delimiter}${process.env.PATH ?? ""}`;
  const env = { ...process.env, PATH: childPath };

  try {
    const child = spawn(runtime.binary, daemonArgs(mode, serverUrl), {
      detached: true,
      stdio: ["ignore", logFd, logFd],
      env,
    });
    child.unref();
    fs.closeSync(logFd);
    return { ok: true, logPath };
  } catch (err) {
    if (logFd !== null) {
      try {
        fs.closeSync(logFd);
      } catch {
        /* ignore */
      }
    }
    return { ok: false, error: `Couldn't start the host: ${err.message}`, logPath };
  }
}

module.exports = {
  // pure / testable
  decideMode,
  isLoopbackServer,
  daemonArgs,
  daemonTarget,
  daemonRecordPath,
  readDaemonRecord,
  computeNeedsLogin,
  resolveOmnigentRuntime,
  clearRuntimeCache,
  // effectful
  getLocalHostStatus,
  startLocalHost,
  // constants (for tests)
  STATE_DIR,
  AUTH_TOKENS_PATH,
  LOCAL_DAEMON_MARKER,
};
