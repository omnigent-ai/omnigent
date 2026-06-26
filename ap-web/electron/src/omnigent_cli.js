// Discovery and invocation of the local `omnigent` CLI for the desktop shell.
//
// The desktop manages servers by shelling out to the same `omnigent` binary a
// user would run by hand — `server start|stop|status` and `host status` (the
// long-lived `host` connection is spawned by server_manager.js, which owns its
// lifetime). This module locates the binary, runs the short exit-quick
// commands, and parses their `--json` output. The CLI is the single source of
// truth for live state; nothing here is persisted.
//
// Unlike src/url.js this is main-process only (it needs child_process / fs),
// so it's a plain CommonJS module — never loaded in the renderer.
//
// The pure helpers (matchesServer, parseDaemonRecord, normalizeServerUrl,
// candidatePaths, resolveCliPath with injected probes) are unit-tested in
// test/omnigent_cli.test.js; the functions that actually spawn a binary are
// exercised in the manual verification flow.

"use strict";

const { execFile, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const yaml = require("js-yaml");

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
 * The Omnigent local runtime data dir — `$OMNIGENT_DATA_DIR` (with `~`
 * expanded) or `~/.omnigent`. Mirrors `_local_data_dir()` in
 * omnigent/host/local_server.py. The local-server pidfile lives here.
 *
 * @returns {string}
 */
function localDataDir() {
  const raw = process.env.OMNIGENT_DATA_DIR;
  if (raw && raw.trim() !== "") {
    const expanded = raw.startsWith("~") ? path.join(os.homedir(), raw.slice(1)) : raw;
    return path.resolve(expanded);
  }
  return path.join(os.homedir(), ".omnigent");
}

/**
 * The Omnigent config dir — `$OMNIGENT_CONFIG_HOME` (with `~` expanded) or
 * `~/.omnigent`. config.yaml (machine identity) lives here; it can differ from
 * the data dir under test env overrides, but is the same by default.
 *
 * @returns {string}
 */
function localConfigDir() {
  const raw = process.env.OMNIGENT_CONFIG_HOME;
  if (raw && raw.trim() !== "") {
    const expanded = raw.startsWith("~") ? path.join(os.homedir(), raw.slice(1)) : raw;
    return path.resolve(expanded);
  }
  return path.join(os.homedir(), ".omnigent");
}

/**
 * The shared Omnigent state dir, ALWAYS `~/.omnigent` — it ignores
 * `$OMNIGENT_DATA_DIR`, mirroring `state_dir()` in
 * sdks/ui/omnigent_ui_sdk/terminal/_config.py and `_HOST_PID_PATH` in
 * omnigent/cli.py (both hardcode `Path.home()/".omnigent"`). The auth-token
 * store and the daemon registry live here — NOT under the data dir. Only the
 * local-server pidfile honors `$OMNIGENT_DATA_DIR` (see {@link localDataDir}).
 *
 * @returns {string}
 */
function stateDir() {
  return path.join(os.homedir(), ".omnigent");
}

/** Memoized machine host id (stable once generated; never cache a null). */
let cachedHostId = null;

/**
 * This machine's Omnigent host id (e.g. "host_ab12…"), read from the machine
 * identity in `config.yaml` (`host: host_id:`, written by
 * omnigent/host/identity.py) — instant, no subprocess. Present once generated,
 * even before connecting to any server. Returns null when no id exists yet;
 * after the first connect it resolves. Lets the renderer match "this machine"
 * against the server's /v1/hosts list and select it after an auto-connect.
 *
 * @returns {string | null}
 */
function localHostId() {
  if (cachedHostId) return cachedHostId;
  try {
    const parsed = yaml.load(fs.readFileSync(path.join(localConfigDir(), "config.yaml"), "utf8"));
    const id = parsed && typeof parsed === "object" ? parsed.host?.host_id : null;
    if (typeof id === "string" && id) cachedHostId = id;
  } catch {
    // No config yet, or unparseable.
  }
  return cachedHostId;
}

/**
 * Parse the local-server pidfile contents: two lines, PID then port. Returns
 * null when malformed. Mirrors `_read_local_server_pid_file()` in
 * omnigent/host/local_server.py.
 *
 * @param {string} text
 * @returns {{ pid: number, port: number } | null}
 */
function parseLocalServerPidfile(text) {
  if (typeof text !== "string") return null;
  const lines = text.trim().split(/\r?\n/);
  if (lines.length < 2) return null;
  const pid = Number.parseInt(lines[0], 10);
  const port = Number.parseInt(lines[1], 10);
  if (!Number.isFinite(pid) || !Number.isFinite(port)) return null;
  return { pid, port };
}

/**
 * True when a process with this pid exists. `process.kill(pid, 0)` sends no
 * signal — it only probes existence: it throws ESRCH when gone, EPERM when the
 * process exists but isn't ours (still alive).
 *
 * @param {number} pid
 * @returns {boolean}
 */
function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return Boolean(err) && err.code === "EPERM";
  }
}

/**
 * Read + parse the local-server pidfile. Returns { pid, port } or null.
 *
 * @returns {{ pid: number, port: number } | null}
 */
function readLocalServerPidfile() {
  let text;
  try {
    text = fs.readFileSync(path.join(localDataDir(), "local_server.pid"), "utf8");
  } catch {
    return null;
  }
  return parseLocalServerPidfile(text);
}

/**
 * Local-server status from the pidfile + a pid-liveness check — no `omnigent
 * server status` subprocess, so it's instant. Returns null when no live local
 * server is recorded.
 *
 * Liveness only (no `/health`): a dead/cleared pidfile correctly reports null,
 * but a stale pidfile whose pid happens to be alive (reused/hung) would report
 * running. That's acceptable for the sidebar row, which is only shown when this
 * port matches the server the window is already connected to — so a server is
 * provably up there. Decisions made WITHOUT that guarantee (e.g. reusing a
 * server before navigating) must use {@link localServerHealthy} instead.
 *
 * @returns {{ running: true, url: string, pid: number, port: number } | null}
 */
function localServerStatus() {
  const rec = readLocalServerPidfile();
  if (!rec || !isPidAlive(rec.pid)) return null;
  return { running: true, url: `http://127.0.0.1:${rec.port}`, pid: rec.pid, port: rec.port };
}

/**
 * Health-verified local-server lookup: pidfile + pid liveness + a `/health`
 * probe (short timeout), mirroring `local_server_url_if_healthy()` in
 * omnigent/host/local_server.py. Returns null for a stale pidfile (dead pid, a
 * reused pid with nothing listening → connection refused fast, or a hung server
 * → times out). Use this before reusing a server you're about to navigate to,
 * so a stale pidfile doesn't send the window to a dead URL.
 *
 * @param {number} [timeoutMs]
 * @returns {Promise<{ url: string, pid: number, port: number } | null>}
 */
async function localServerHealthy(timeoutMs = 1500) {
  const rec = readLocalServerPidfile();
  if (!rec || !isPidAlive(rec.pid)) return null;
  const url = `http://127.0.0.1:${rec.port}`;
  try {
    const resp = await fetch(`${url}/health`, { signal: AbortSignal.timeout(timeoutMs) });
    if (resp.ok) return { url, pid: rec.pid, port: rec.port };
  } catch {
    // Refused / unreachable / timed out → not a healthy server we can reuse.
  }
  return null;
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
 * Whether the CLI holds valid stored credentials for a server — read straight
 * from `~/.omnigent/auth_tokens.json` (no subprocess), mirroring
 * omnigent/cli_auth.py: keyed by the trailing-slash-stripped URL, a record is
 * valid if it's a Databricks pointer (has `workspace_host`) or a non-expired
 * session token. The CLI's `state_dir()` is hardcoded to `~/.omnigent`.
 *
 * @param {string} serverUrl
 * @returns {boolean}
 */
function serverAuthed(serverUrl) {
  if (typeof serverUrl !== "string" || serverUrl === "") return false;
  const key = serverUrl.replace(/\/+$/, "");
  let data;
  try {
    data = JSON.parse(fs.readFileSync(path.join(stateDir(), "auth_tokens.json"), "utf8"));
  } catch {
    return false;
  }
  const entry = data && typeof data === "object" ? data[key] : null;
  if (!entry || typeof entry !== "object") return false;
  if (entry.auth_type === "databricks") {
    return typeof entry.workspace_host === "string" && entry.workspace_host !== "";
  }
  if (typeof entry.token === "string" && entry.token !== "") {
    // expires_at is unix seconds (cli_auth uses time.time()); treat absent as
    // non-expiring.
    return typeof entry.expires_at === "number" ? entry.expires_at >= Date.now() / 1000 : true;
  }
  return false;
}

/**
 * Run `omnigent login <serverUrl>` to authenticate the CLI to a server. It's a
 * no-op when the server needs no auth (header mode), opens the system browser
 * for OIDC / Databricks, and fails fast for password (TTY) modes when run
 * without a terminal. Long timeout to allow the interactive browser flow.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @param {{ timeoutMs?: number }} [opts]
 * @returns {Promise<{ ok: boolean, output: string }>}
 */
async function loginServer(cliPath, serverUrl, { timeoutMs = 180000 } = {}) {
  const res = await runCli(cliPath, ["login", serverUrl], { timeoutMs });
  return { ok: res.code === 0, output: (res.stdout || res.stderr).trim() };
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
 * True when a daemon record refers to the given server URL. Compares its
 * `server_url`, `target`, AND `resolved_server_url` (after trailing-slash
 * normalization) — the last matters for a local-mode daemon (target `"local"`,
 * server_url null) whose loopback URL only appears as `resolved_server_url`, so
 * connecting by that loopback URL still recognizes it.
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
    normalizeServerUrl(daemon.server_url) === want ||
    normalizeServerUrl(daemon.target) === want ||
    normalizeServerUrl(daemon.resolved_server_url) === want
  );
}

/**
 * Directory holding per-target daemon registry records, mirroring
 * `_daemon_registry_dir()` in omnigent/cli.py (`<state_dir>/daemons`).
 *
 * @returns {string}
 */
function daemonRegistryDir() {
  return path.join(stateDir(), "daemons");
}

/**
 * Parse one decoded daemon registry record into the subset the desktop needs.
 * Mirrors the validation in `_record_from_json()` (omnigent/cli.py): a usable
 * record needs a positive integer `pid`, a non-empty `target`, and a known
 * `mode`. Returns null for malformed records.
 *
 * @param {unknown} raw
 * @returns {{
 *   pid: number,
 *   target: string,
 *   mode: "local" | "server",
 *   server_url: string | null,
 *   resolved_server_url: string | null,
 *   host_id: string | null,
 *   log_path: string | null,
 * } | null}
 */
function parseDaemonRecord(raw) {
  if (!raw || typeof raw !== "object") return null;
  const pid =
    typeof raw.pid === "number"
      ? raw.pid
      : typeof raw.pid === "string"
        ? Number.parseInt(raw.pid, 10)
        : NaN;
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const target = typeof raw.target === "string" ? raw.target : "";
  const mode = raw.mode === "local" || raw.mode === "server" ? raw.mode : "";
  if (!target || !mode) return null;
  const str = (v) => (typeof v === "string" && v ? v : null);
  return {
    pid,
    target,
    mode,
    server_url: str(raw.server_url),
    resolved_server_url: str(raw.resolved_server_url),
    host_id: str(raw.host_id),
    log_path: str(raw.log_path),
  };
}

/**
 * Read every daemon registry record from disk (`~/.omnigent/daemons/*.json`).
 * This is the fast substitute for `omnigent host status --json`: it gives the
 * daemon metadata and (with a pid-liveness check) process state without the
 * per-session runner probes that make the CLI command slow. Tunnel health
 * ({@link probeHostTunnel}) is layered on separately. Returns [] when the
 * registry is absent.
 *
 * @returns {ReturnType<typeof parseDaemonRecord>[]}
 */
function readDaemonRecords() {
  const dir = daemonRegistryDir();
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const records = [];
  for (const name of names) {
    if (!name.endsWith(".json")) continue;
    try {
      const raw = JSON.parse(fs.readFileSync(path.join(dir, name), "utf8"));
      const rec = parseDaemonRecord(raw);
      if (rec) records.push(rec);
    } catch {
      // Skip an unreadable/garbage record — a half-written file mid-rotation.
    }
  }
  return records;
}

/**
 * The Omnigent server URL a daemon record talks to, mirroring
 * `_daemon_base_url()` (omnigent/cli.py): a local-mode daemon's URL lives in
 * `resolved_server_url` (falling back to a healthy local server's URL); a
 * server-mode daemon's is its `server_url`/`target`.
 *
 * @param {ReturnType<typeof parseDaemonRecord>} record
 * @returns {string | null}
 */
function daemonServerUrl(record) {
  if (!record) return null;
  if (record.mode === "local") {
    if (record.resolved_server_url) return record.resolved_server_url.replace(/\/+$/, "");
    return localServerStatus()?.url ?? null;
  }
  return (record.server_url || record.target).replace(/\/+$/, "");
}

/**
 * The bearer token to authenticate an in-process request to `serverUrl`: a
 * non-expired session token stored by `omnigent login` in `auth_tokens.json`,
 * looked up by the exact server URL. Returns null for a Databricks-pointer login
 * (no token is stored — the SDK mints one per request) or when nothing is stored
 * for this URL.
 *
 * Unlike `_remote_headers()` (omnigent/chat.py), this deliberately does NOT
 * honor `OMNIGENT_REMOTE_AUTH_TOKEN`. That token is destination-independent — it
 * authenticates to whatever URL it's sent to — and this function's only caller
 * (the status probe) targets a URL adjacent to an on-disk daemon record, so a
 * destination-blind token there could be sent to an attacker URL planted in
 * `~/.omnigent/daemons/`. The desktop never needs it: it's not inherited by a
 * GUI launch and the per-URL stored token below covers the real auth path. A
 * desktop started with the env var set just falls to the optimistic/unverified
 * status path — already the behavior for SDK / Databricks-pointer auth.
 *
 * @param {string} serverUrl
 * @returns {string | null}
 */
function bearerTokenFor(serverUrl) {
  if (typeof serverUrl !== "string" || serverUrl === "") return null;
  const key = serverUrl.replace(/\/+$/, "");
  let data;
  try {
    data = JSON.parse(fs.readFileSync(path.join(stateDir(), "auth_tokens.json"), "utf8"));
  } catch {
    return null;
  }
  const entry = data && typeof data === "object" ? data[key] : null;
  if (!entry || typeof entry !== "object") return null;
  if (typeof entry.token === "string" && entry.token !== "") {
    if (typeof entry.expires_at === "number" && entry.expires_at < Date.now() / 1000) return null;
    return entry.token;
  }
  return null;
}

/**
 * The "basic request" that detects whether a host's tunnel is up: a single
 * `GET {serverUrl}/v1/hosts/{host_id}`, reading `body.status` — the same probe
 * `_add_daemon_host_status()` (omnigent/cli.py) makes, minus the per-session
 * runner enumeration. Loopback servers are single-user (no auth); a remote
 * server needs a bearer ({@link bearerTokenFor}). When no bearer is obtainable
 * in-process (a Databricks-pointer login, or auth supplied only via the SDK /
 * the env var the desktop no longer reads), returns `authMissing` so the caller
 * can avoid falsely reporting the tunnel down.
 *
 * @param {string} serverUrl
 * @param {string | null} hostId
 * @param {{ timeoutMs?: number }} [opts]
 * @returns {Promise<{ status: string | null, reachable: boolean, authMissing: boolean }>}
 */
async function probeHostTunnel(serverUrl, hostId, { timeoutMs = 2000 } = {}) {
  if (typeof serverUrl !== "string" || !serverUrl || typeof hostId !== "string" || !hostId) {
    return { status: null, reachable: false, authMissing: false };
  }
  const headers = {};
  // A loopback server is usually single-user (no auth), but send a stored token
  // if one happens to exist so an authed loopback server still verifies. A
  // remote server with no obtainable token can't be probed in-process (a
  // Databricks-pointer login mints tokens via the SDK) → report authMissing so
  // the caller falls back to the optimistic/unverified path, not "offline".
  const token = bearerTokenFor(serverUrl);
  if (token) headers.Authorization = `Bearer ${token}`;
  else if (!isLoopbackServer(serverUrl))
    return { status: null, reachable: false, authMissing: true };
  const base = serverUrl.replace(/\/+$/, "");
  const target = `${base}/v1/hosts/${encodeURIComponent(hostId)}`;
  try {
    const resp = await fetch(target, { headers, signal: AbortSignal.timeout(timeoutMs) });
    if (!resp.ok) return { status: null, reachable: true, authMissing: false };
    const body = await resp.json().catch(() => null);
    const status = body && typeof body.status === "string" ? body.status : null;
    return { status, reachable: true, authMissing: false };
  } catch {
    // Connection refused / unreachable / timed out → can't confirm the tunnel.
    return { status: null, reachable: false, authMissing: false };
  }
}

/**
 * This machine's connection to `serverUrl`, resolved WITHOUT the slow `omnigent
 * host status` subprocess: daemon metadata + process state come from the
 * on-disk registry ({@link readDaemonRecords}), and tunnel health from one
 * basic request ({@link probeHostTunnel}). Returns `{ connected, process,
 * hostStatus, pid, error }` plus `verified` (false when the tunnel couldn't be
 * probed — e.g. Databricks-pointer auth — so process-alive is reported
 * optimistically rather than as offline).
 *
 * @param {string} serverUrl
 * @param {{ probe?: boolean, timeoutMs?: number }} [opts]
 * @returns {Promise<{
 *   connected: boolean,
 *   process: "online" | "offline",
 *   hostStatus: string | null,
 *   pid: number | null,
 *   error: string | null,
 *   verified: boolean,
 * }>}
 */
async function getHostConnectionFast(serverUrl, { probe = true, timeoutMs = 2000 } = {}) {
  const match = readDaemonRecords().find((r) => matchesServer(r, serverUrl)) || null;
  if (!match) {
    return {
      connected: false,
      process: "offline",
      hostStatus: null,
      pid: null,
      error: null,
      verified: true,
    };
  }
  if (!isPidAlive(match.pid)) {
    return {
      connected: false,
      process: "offline",
      hostStatus: null,
      pid: match.pid,
      error: null,
      verified: true,
    };
  }
  // Process is alive. Without a tunnel probe we can only attest the process.
  if (!probe) {
    return {
      connected: true,
      process: "online",
      hostStatus: null,
      pid: match.pid,
      error: null,
      verified: false,
    };
  }
  const hostId = match.host_id || localHostId();
  // Probe the very server URL this window is connected to — NEVER a URL
  // re-derived from the daemon record via daemonServerUrl(match). matchesServer
  // only confirms the record *names* serverUrl in one of its fields; a
  // planted/edited record whose `target` matches the real server but whose
  // `server_url` points at evil.com would still match here, and
  // daemonServerUrl(match) would then send this probe — and our host_id — to the
  // attacker URL. Pinning it to serverUrl keeps the request on the destination
  // the user actually chose. (bearerTokenFor is also URL-keyed and no longer
  // honors the destination-blind env token, so no credential can follow a probe
  // to an unexpected host.)
  const res = await probeHostTunnel(serverUrl, hostId, { timeoutMs });
  if (res.authMissing) {
    // Can't reproduce Databricks-pointer auth in-process → report the live
    // process optimistically as connected, flagged unverified.
    return {
      connected: true,
      process: "online",
      hostStatus: null,
      pid: match.pid,
      error: null,
      verified: false,
    };
  }
  if (!res.reachable) {
    return {
      connected: false,
      process: "online",
      hostStatus: null,
      pid: match.pid,
      error: "server unreachable",
      verified: true,
    };
  }
  return {
    connected: res.status === "online",
    process: "online",
    hostStatus: res.status,
    pid: match.pid,
    error: null,
    verified: true,
  };
}

module.exports = {
  INSTALL_COMMAND,
  DEFAULT_TIMEOUT_MS,
  normalizeServerUrl,
  isLoopbackServer,
  sameLoopbackServer,
  localHostId,
  parseLocalServerPidfile,
  isPidAlive,
  readLocalServerPidfile,
  localServerStatus,
  localServerHealthy,
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
  stopHost,
  serverAuthed,
  loginServer,
  matchesServer,
  daemonRegistryDir,
  parseDaemonRecord,
  readDaemonRecords,
  daemonServerUrl,
  bearerTokenFor,
  probeHostTunnel,
  getHostConnectionFast,
};
