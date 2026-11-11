// Discovery and invocation of the local `omnigent` CLI for the desktop shell.
//
// The desktop manages servers by shelling out to the same `omnigent` binary a
// user would run by hand — `server --background/stop/status` and `host status` (the
// long-lived `host` connection is spawned by server_manager.ts, which owns its
// lifetime). This module locates the binary, runs the short exit-quick
// commands, and parses their `--json` output. The CLI is the single source of
// truth for live state; nothing here is persisted.
//
// Unlike src/url.ts this is main-process only (it needs child_process / fs),
// so it's never loaded in the renderer.
//
// The pure helpers (matchesServer, parseDaemonRecord, normalizeServerUrl,
// candidatePaths, resolveCliPath with injected probes) are unit-tested in
// test/omnigent_cli.test.ts, as is runCli's failure classification against real
// subprocesses; the other spawning functions are exercised in the manual
// verification flow.

import { execFile, execFileSync, type ExecFileException } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { load as loadYaml } from "js-yaml";

import * as url from "./url.ts";

/** Default timeout for the short status commands. */
export const DEFAULT_TIMEOUT_MS = 10000;

/** Ceiling for `login`, just past the CLI's own 300s OIDC ticket deadline. */
export const LOGIN_TIMEOUT_MS = 305000;

/**
 * One-liner shown on the setup page when the CLI is missing. Mirrors the
 * install instructions in the repo root README.
 */
export const INSTALL_COMMAND =
  "curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh";

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** `new URL(value)` on an untrusted value. */
function parseUrl(value: unknown): URL {
  // The constructor stringifies any value itself; the cast only lets TypeScript pass it.
  return new URL(value as string);
}

/**
 * Strip a trailing slash so URL comparisons survive the difference between
 * what the user typed and what the CLI records in a daemon target.
 */
export function normalizeServerUrl(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.trim().replace(/\/+$/, "");
}

/**
 * True when a server URL points at the local machine — loopback host. Only
 * loopback servers expose the local-server background/stop controls. Reuses the
 * shared LOCAL_HOSTS set from url.ts so the desktop never disagrees on what
 * "local" means.
 */
export function isLoopbackServer(serverUrl: string): boolean {
  try {
    return url.LOCAL_HOSTS.has(new URL(serverUrl).hostname);
  } catch {
    return false;
  }
}

/**
 * True when two URLs refer to the same local server — both loopback hosts on
 * the same endpoint with only the loopback hostname substituted. Used to
 * confirm the CLI's local server is the one a window is actually connected
 * to before showing its controls. Either side may be an unchecked record field.
 */
export function sameLoopbackServer(a: unknown, b: unknown): boolean {
  try {
    const ua = parseUrl(a);
    const ub = parseUrl(b);
    if (!url.LOCAL_HOSTS.has(ua.hostname) || !url.LOCAL_HOSTS.has(ub.hostname)) return false;
    ua.hostname = ub.hostname;
    return normalizeServerUrl(ua.href) === normalizeServerUrl(ub.href);
  } catch {
    return false;
  }
}

/**
 * The Omnigent local runtime data dir — `$OMNIGENT_DATA_DIR` (with `~`
 * expanded) or `~/.omnigent`. Mirrors `_local_data_dir()` in
 * omnigent/host/local_server.py. The local-server pidfile lives here.
 */
function localDataDir(): string {
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
 */
function localConfigDir(): string {
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
 */
function stateDir(): string {
  return path.join(os.homedir(), ".omnigent");
}

/** Memoized machine host id (stable once generated; never cache a null). */
let cachedHostId: string | null = null;

/**
 * This machine's Omnigent host id (bare 32-char hex, e.g. "ab12…"), read from
 * the machine identity in `config.yaml` (`host: host_id:`, written by
 * omnigent/host/identity.py) — instant, no subprocess. Present once generated,
 * even before connecting to any server. Returns null when no id exists yet;
 * after the first connect it resolves. Lets the renderer match "this machine"
 * against the server's /v1/hosts list and select it after an auto-connect.
 *
 * Installs predating the drop of the `host_` prefix still hold the legacy
 * spelling on disk, while /v1/hosts always reports the bare form (`hosts.host_id`
 * is a Uuid16 column). The renderer compares these as plain strings — it is the
 * one consumer the server's own prefix-tolerance can't rescue — so strip the
 * prefix here, mirroring `_normalize_host_id` in omnigent/host/identity.py.
 */
export function localHostId(): string | null {
  if (cachedHostId) return cachedHostId;
  cachedHostId = readLocalHostId();
  return cachedHostId;
}

/** Read the current machine identity without retaining a stale config value. */
export function readLocalHostId(): string | null {
  try {
    const parsed = loadYaml(fs.readFileSync(path.join(localConfigDir(), "config.yaml"), "utf8"));
    const id =
      parsed && typeof parsed === "object"
        ? propertyOf(propertyOf(parsed, "host"), "host_id")
        : null;
    if (typeof id === "string" && id) return id.replace(/^host_/, "") || null;
  } catch {
    // No config yet, or unparseable.
  }
  return null;
}

/** The PID and port recorded in the local-server pidfile. */
export interface LocalServerPidfile {
  pid: number;
  port: number;
}

/**
 * Parse the local-server pidfile contents: two lines, PID then port. Returns
 * null when malformed. Mirrors `_read_local_server_pid_file()` in
 * omnigent/host/local_server.py.
 */
export function parseLocalServerPidfile(text: unknown): LocalServerPidfile | null {
  if (typeof text !== "string") return null;
  const [pidLine, portLine] = text.trim().split(/\r?\n/);
  if (pidLine === undefined || portLine === undefined) return null;
  const pid = Number.parseInt(pidLine, 10);
  const port = Number.parseInt(portLine, 10);
  if (!Number.isFinite(pid) || !Number.isFinite(port)) return null;
  return { pid, port };
}

/**
 * True when a process with this pid exists. `process.kill(pid, 0)` sends no
 * signal — it only probes existence: it throws ESRCH when gone, EPERM when the
 * process exists but isn't ours (still alive).
 */
export function isPidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return Boolean(err) && propertyOf(err, "code") === "EPERM";
  }
}

/** Read + parse the local-server pidfile. Returns { pid, port } or null. */
export function readLocalServerPidfile(): LocalServerPidfile | null {
  let text;
  try {
    text = fs.readFileSync(path.join(localDataDir(), "local_server.pid"), "utf8");
  } catch {
    return null;
  }
  return parseLocalServerPidfile(text);
}

/** A local server recorded as running, with the URL it serves. */
export interface LocalServerStatus extends LocalServerPidfile {
  running: true;
  url: string;
}

/** A local server whose `/health` answered. */
export interface HealthyLocalServer extends LocalServerPidfile {
  url: string;
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
 */
export function localServerStatus(): LocalServerStatus | null {
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
 */
export async function localServerHealthy(timeoutMs = 1500): Promise<HealthyLocalServer | null> {
  const rec = readLocalServerPidfile();
  if (!rec || !isPidAlive(rec.pid)) return null;
  const localUrl = `http://127.0.0.1:${rec.port}`;
  try {
    const resp = await fetch(`${localUrl}/health`, { signal: AbortSignal.timeout(timeoutMs) });
    if (resp.ok) return { url: localUrl, pid: rec.pid, port: rec.port };
  } catch {
    // Refused / unreachable / timed out → not a healthy server we can reuse.
  }
  return null;
}

/**
 * The CLI binary's two console-script names — both resolve to the same entry
 * point (`omnigent.cli:main`); `omni` is the short alias. We probe `omnigent`
 * first (canonical) but accept `omni` so a machine that only installed the
 * alias still resolves. See pyproject.toml `[project.scripts]`.
 */
const CLI_NAMES = ["omnigent", "omni"];

/**
 * Well-known install locations for the CLI binary, in priority order. For each
 * directory we list the `omnigent` name then the `omni` alias.
 * `uv tool install` (the documented installer) drops it in ~/.local/bin;
 * the rest cover Homebrew and source/cargo installs. Probing these matters
 * because a GUI-launched Electron app inherits a minimal PATH that usually
 * omits ~/.local/bin, so `command -v` alone is not enough.
 */
export function candidatePaths(): string[] {
  const home = os.homedir();
  const dirs = [
    path.join(home, ".local", "bin"),
    path.join(home, ".cargo", "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
  ];
  return dirs.flatMap((dir) => CLI_NAMES.map((name) => path.join(dir, name)));
}

/** True when `p` exists, is a regular file, and is executable by this process. */
export function isExecutableFile(p: string): boolean {
  try {
    if (!fs.statSync(p).isFile()) return false;
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolve the CLI on PATH (or the user's login shell PATH) by name. Returns
 * null when not found. On POSIX we go through `command -v` so shell-managed
 * PATHs (uv shims) resolve; on Windows we use `where`.
 *
 * @param name e.g. "omnigent" or "omni"
 */
function whichName(name: string): string | null {
  try {
    if (process.platform === "win32") {
      const out = execFileSync("where", [name], { encoding: "utf8" });
      return out.trim().split(/\r?\n/)[0] || null;
    }
    const out = execFileSync("/bin/sh", ["-c", `command -v ${name}`], {
      encoding: "utf8",
    });
    return out.trim() || null;
  } catch {
    return null;
  }
}

/**
 * Resolve the CLI on PATH, trying `omnigent` then the `omni` alias. Returns the
 * first hit, or null when neither is on PATH.
 */
export function whichOmnigent(): string | null {
  for (const name of CLI_NAMES) {
    const found = whichName(name);
    if (found) return found;
  }
  return null;
}

/** The executability and PATH probes behind {@link resolveCliPath}. */
export interface ResolveCliPathDeps {
  isExecutableFile?: ((p: string) => boolean) | undefined;
  whichOmnigent?: (() => string | null) | undefined;
  candidatePaths?: (() => string[]) | undefined;
}

/** A usable CLI binary and which lookup found it. */
export interface ResolvedCliPath {
  path: string;
  source: "configured" | "path" | "candidate";
}

/**
 * Locate the `omnigent` binary. Resolution order: a user-configured path, then
 * PATH, then the well-known candidate locations. Returns the resolved path and
 * which source matched, or null if nothing usable was found.
 *
 * `deps` lets the tests inject the executability/PATH probes so the resolution
 * order can be verified without a real binary on disk.
 *
 * @param configuredPath settings.omnigent_path
 */
export function resolveCliPath(
  configuredPath: string | null | undefined,
  deps: ResolveCliPathDeps = {},
): ResolvedCliPath | null {
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

/** An executable with a fixed argv prefix, e.g. a managed host's CLI. */
export interface CliCommandDescriptor {
  executable: string;
  prefixArgs?: string[] | undefined;
  displayName?: string | undefined;
}

/** A CLI path, or an executable with a fixed argv prefix. */
export type CliCommand = string | CliCommandDescriptor;

/** A validated CLI invocation. */
export interface CliCommandParts {
  executable: string;
  prefixArgs: string[];
  displayName: string;
}

/** The descriptor fields of an unchecked object, before validation. */
interface UncheckedCliCommand {
  executable?: unknown;
  prefixArgs?: unknown;
  displayName?: unknown;
}

/**
 * Normalize a CLI invocation. A plain path passes a string (`/path/omnigent`);
 * managed host enrollment passes an executable + fixed argv prefix
 * (`/usr/local/bin/isaac`, `["omni"]`). Both stay shell-free. Callers pass
 * settings values unchecked, so anything else throws.
 */
export function cliCommandParts(command: unknown): CliCommandParts {
  if (typeof command === "string" && command !== "") {
    return { executable: command, prefixArgs: [], displayName: "omnigent" };
  }
  // Only read after the object check below; the cast lets TypeScript name the fields.
  const descriptor = command as UncheckedCliCommand;
  if (
    command &&
    typeof command === "object" &&
    typeof descriptor.executable === "string" &&
    descriptor.executable !== "" &&
    (descriptor.prefixArgs === undefined ||
      (Array.isArray(descriptor.prefixArgs) &&
        descriptor.prefixArgs.every((arg): arg is string => typeof arg === "string")))
  ) {
    const prefixArgs: string[] = descriptor.prefixArgs ?? [];
    return {
      executable: descriptor.executable,
      prefixArgs,
      displayName:
        typeof descriptor.displayName === "string" && descriptor.displayName !== ""
          ? descriptor.displayName
          : [path.basename(descriptor.executable), ...prefixArgs].join(" "),
    };
  }
  throw new TypeError("invalid CLI command");
}

/**
 * Why a CLI subprocess failed, built only from process-level facts so it is
 * safe to show or log: CLI output can carry auth material and is never read.
 */
export interface CliFailure {
  kind: "cli-not-found" | "spawn-failed" | "timed-out" | "signal" | "exited" | "failed";
  exitCode?: number;
  signal?: string;
  errorCode?: string;
}

/**
 * Classify an `execFile` error. execFile sets `err.code` to the numeric exit
 * code on a normal non-zero exit, or a string errno (e.g. "ENOENT") with a
 * `spawn …` syscall when the spawn itself failed; `killed` marks its own
 * timeout kill, which a child that handles the signal can still answer with an
 * exit code instead of dying by the signal.
 */
function cliFailureOf(err: ExecFileException): CliFailure {
  if (err.killed) {
    return {
      kind: "timed-out",
      ...(typeof err.code === "number" ? { exitCode: err.code } : {}),
      ...(typeof err.signal === "string" ? { signal: err.signal } : {}),
    };
  }
  if (typeof err.code === "number") return { kind: "exited", exitCode: err.code };
  if (
    typeof err.code === "string" &&
    typeof err.syscall === "string" &&
    err.syscall.startsWith("spawn")
  ) {
    return { kind: err.code === "ENOENT" ? "cli-not-found" : "spawn-failed", errorCode: err.code };
  }
  if (typeof err.signal === "string") return { kind: "signal", signal: err.signal };
  return { kind: "failed" };
}

/** A per-call timeout for a CLI subprocess. */
export interface CliTimeoutOptions {
  timeoutMs?: number | undefined;
}

/** A finished CLI subprocess. */
export interface CliRunResult {
  code: number;
  stdout: string;
  stderr: string;
  failure?: CliFailure;
}

/**
 * Run an Omnigent CLI subcommand and resolve with its captured output. Never
 * rejects — a failure surfaces as a non-zero `code` (1 when there's no exit
 * code) plus stderr and a {@link CliFailure} so callers can decide. `execFile`
 * (no shell) avoids quoting pitfalls. An invalid command throws before any spawn.
 */
export function runCli(
  command: CliCommand,
  args: string[],
  { timeoutMs = DEFAULT_TIMEOUT_MS }: CliTimeoutOptions = {},
): Promise<CliRunResult> {
  const { executable, prefixArgs } = cliCommandParts(command);
  return new Promise((resolve) => {
    execFile(
      executable,
      [...prefixArgs, ...args],
      { timeout: timeoutMs, encoding: "utf8" },
      (err, stdout, stderr) => {
        const code = err ? (typeof err.code === "number" ? err.code : 1) : 0;
        resolve({
          code,
          stdout: stdout || "",
          stderr: stderr || "",
          ...(err ? { failure: cliFailureOf(err) } : {}),
        });
      },
    );
  });
}

/**
 * Whether the CLI holds valid stored credentials for a server — read straight
 * from `~/.omnigent/auth_tokens.json` (no subprocess), mirroring
 * omnigent/cli_auth.py: keyed by the trailing-slash-stripped URL, a record is
 * valid if it's a Databricks pointer (has `workspace_host`) or a non-expired
 * session token. The CLI's `state_dir()` is hardcoded to `~/.omnigent`.
 */
export function serverAuthed(serverUrl: string): boolean {
  if (typeof serverUrl !== "string" || serverUrl === "") return false;
  const key = serverUrl.replace(/\/+$/, "");
  let data: unknown;
  try {
    data = JSON.parse(fs.readFileSync(path.join(stateDir(), "auth_tokens.json"), "utf8"));
  } catch {
    return false;
  }
  const entry = data && typeof data === "object" ? propertyOf(data, key) : null;
  if (!entry || typeof entry !== "object") return false;
  if (propertyOf(entry, "auth_type") === "databricks") {
    const workspaceHost = propertyOf(entry, "workspace_host");
    return typeof workspaceHost === "string" && workspaceHost !== "";
  }
  const token = propertyOf(entry, "token");
  if (typeof token === "string" && token !== "") {
    // expires_at is unix seconds (cli_auth uses time.time()); treat absent as
    // non-expiring.
    const expiresAt = propertyOf(entry, "expires_at");
    return typeof expiresAt === "number" ? expiresAt >= Date.now() / 1000 : true;
  }
  return false;
}

/** Whether a CLI command exited cleanly, with its trimmed output. */
export interface CliCommandResult {
  ok: boolean;
  output: string;
}

/** A finished `login`, with the process-level failure when it failed. */
export interface CliLoginResult extends CliCommandResult {
  failure?: CliFailure;
}

/**
 * Run `omnigent login <serverUrl>` to authenticate the CLI to a server. It's a
 * no-op when the server needs no auth (header mode), opens the system browser
 * for OIDC / Databricks, and fails fast for password (TTY) modes when run
 * without a terminal. The default timeout is generous (~300s) so a human
 * completing the browser sign-in isn't SIGKILLed mid-flow — the CLI's own OIDC
 * ticket deadline (300s, `_CLI_LOGIN_TIMEOUT_SECONDS` in omnigent/cli.py)
 * governs first.
 */
export async function loginServer(
  cliPath: CliCommand,
  serverUrl: string,
  { timeoutMs = LOGIN_TIMEOUT_MS }: CliTimeoutOptions = {},
): Promise<CliLoginResult> {
  const res = await runCli(cliPath, ["login", serverUrl], { timeoutMs });
  return {
    ok: res.code === 0,
    output: (res.stdout || res.stderr).trim(),
    ...(res.failure ? { failure: res.failure } : {}),
  };
}

/**
 * Parse the first JSON object out of CLI stdout. The status commands emit a
 * single JSON blob, but tolerate a stray leading warning line by falling back
 * to the outermost `{…}` slice. Returns null when nothing parses; any other
 * JSON value is returned unchecked.
 */
export function parseJsonLoose(stdout: string | null | undefined): unknown {
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

/** Whether a usable CLI is installed, and how to install one. */
export interface CliStatus {
  installed: boolean;
  path: string | null;
  version: string | null;
  source: ResolvedCliPath["source"] | null;
  installCommand: string;
}

/**
 * Probe the CLI and report whether it's installed and usable. Validates the
 * resolved path by actually running `--version`, so a stale or wrong configured
 * path reports `installed:false` rather than failing later.
 */
export async function getCliStatus(configuredPath: string | null | undefined): Promise<CliStatus> {
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
  const version = res.stdout.trim() || res.stderr.trim() || "";
  // Must exit cleanly AND identify itself as omni — `omnigent --version` prints
  // e.g. "omnigent 0.3.0.dev0 (…)". The exit-code alone isn't enough: an
  // unrelated binary (e.g. /bin/echo) also exits 0 on `--version`, and we must
  // not accept it as the CLI (it would later fail to run a server / host).
  const ok = res.code === 0 && /\bomni/i.test(version);
  return {
    installed: ok,
    path: ok ? resolved.path : null,
    version: ok ? version || null : null,
    source: ok ? resolved.source : null,
    installCommand: INSTALL_COMMAND,
  };
}

/**
 * `omnigent server status --json`. Returns the parsed payload, or a synthetic
 * not-running shape when the command produced no JSON. The payload is
 * unchecked CLI output.
 */
export async function getServerStatus(cliPath: CliCommand): Promise<unknown> {
  const res = await runCli(cliPath, ["server", "status", "--json"]);
  const json = parseJsonLoose(res.stdout);
  if (!json) {
    return { running: false, error: res.stderr.trim() || "could not read server status" };
  }
  return json;
}

/** A local server the CLI reports running after a start. */
export interface LocalServerStarted {
  ok: true;
  url: string;
  port: unknown;
  pid: unknown;
}

/** A failed CLI operation and its message. */
export interface CliError {
  ok: false;
  error: string;
}

export type StartLocalServerResult = LocalServerStarted | CliError;

/**
 * Start (or reuse) the local background server, then re-read status for a
 * reliable URL. `server --background` is idempotent on the CLI side. The
 * port and pid are passed through from the status output unchecked.
 */
export async function startLocalServer(cliPath: CliCommand): Promise<StartLocalServerResult> {
  const res = await runCli(cliPath, ["server", "--background"], { timeoutMs: 30000 });
  const status = await getServerStatus(cliPath);
  const statusUrl = status ? propertyOf(status, "url") : undefined;
  if (status && propertyOf(status, "running") && typeof statusUrl === "string") {
    return {
      ok: true,
      url: statusUrl,
      port: propertyOf(status, "port"),
      pid: propertyOf(status, "pid"),
    };
  }
  return {
    ok: false,
    error: res.stderr.trim() || res.stdout.trim() || "failed to start the local server",
  };
}

/** Directory the background server writes its per-run logfile into. Mirrors
 * `open_process_log_file("server", root=<data_dir>/logs)` in local_server.py:
 * files are named `server-<timestamp>.log` and created at spawn time. */
function localServerLogDir(): string {
  return path.join(localDataDir(), "logs", "server");
}

/**
 * Find the background server's CURRENT-run logfile by scanning the server log
 * dir for the newest `server-*.log` created at/after `startedAtMs`.
 *
 * Why not the `local_server.logpath` sidecar: that sidecar is written only
 * AFTER the server is healthy (local_server.py records it post-`/health`), so
 * during boot it doesn't exist yet — tailing it can't stream the startup, and a
 * stale sidecar from a prior crashed run would stream old content. The child's
 * real logfile, by contrast, is created the moment it spawns. The `startedAtMs`
 * floor rejects a previous run's log so a failed start never tails stale lines.
 *
 * @param startedAtMs Only consider files modified at/after this (ms).
 * @returns Absolute path of the freshest matching log, or null.
 */
export function findLiveServerLog(startedAtMs: number): string | null {
  const dir = localServerLogDir();
  let entries;
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return null; // dir not created yet (child hasn't spawned)
  }
  let best: string | null = null;
  let bestMtime = -1;
  // A small floor tolerance: the child may open its log a beat before our
  // recorded start, and mtime resolution is coarse on some filesystems.
  const floor = startedAtMs - 2000;
  for (const name of entries) {
    if (!/^server-.*\.log$/.test(name)) continue;
    const full = path.join(dir, name);
    try {
      const st = fs.statSync(full);
      const mtime = st.mtimeMs;
      if (mtime >= floor && mtime > bestMtime) {
        best = full;
        bestMtime = mtime;
      }
    } catch {
      // Racing file removal — skip it.
    }
  }
  return best;
}

// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;]*m/g;

/**
 * Strip ANSI SGR/escape sequences so log lines render as plain text in the
 * setup terminal (uvicorn/app logs may be colorized).
 */
export function stripAnsi(s: string): string {
  return s.replace(ANSI_RE, "");
}

/** Receives chunks of a growing stream. */
export interface LineSplitter {
  push: (chunk: string) => void;
}

/**
 * Split a growing byte stream into whole lines across chunk boundaries. Feed
 * each chunk to `push`; complete lines (newline-terminated) are emitted via
 * `onLine`, and a trailing partial line is buffered until its newline arrives.
 * The classic bug this avoids: emitting a half line when a chunk splits mid-line.
 */
export function makeLineSplitter(onLine: (line: string) => void): LineSplitter {
  let buf = "";
  return {
    push(chunk) {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        // Trim a trailing \r for CRLF logs; keep other content verbatim.
        onLine(buf.slice(0, nl).replace(/\r$/, ""));
        buf = buf.slice(nl + 1);
      }
    },
  };
}

export interface TailLocalServerLogOptions {
  /** Stops the tail; the caller aborts it once the start resolves. */
  signal?: AbortSignal | undefined;
  /** Floors log discovery so stale logs are ignored (default: now). */
  startedAtMs?: number | undefined;
  discoverTimeoutMs?: number | undefined;
  pollMs?: number | undefined;
}

/**
 * Tail the background server's boot logfile, forwarding each new line to
 * `onLine`, until the caller signals stop (its `omnigent server --background`
 * resolved — success or failure) or a discovery timeout elapses. Does NOT own
 * the server process nor decide readiness — it only reads the logfile the
 * detached child writes, keeping the daemon model intact.
 *
 * Discovery uses {@link findLiveServerLog} (the child's real logfile, created
 * at spawn) rather than the post-health `local_server.logpath` sidecar — so
 * lines stream DURING boot, and a prior run's stale log is never adopted. Naive
 * read-from-offset driven by `fs.watch` + a poll fallback; no tail library.
 *
 * @returns resolves when tailing stops (aborted, timed out, or the log couldn't
 *   be found).
 */
export async function tailLocalServerLog(
  onLine: (line: string) => void,
  opts: TailLocalServerLogOptions = {},
): Promise<void> {
  const { signal, startedAtMs = Date.now(), discoverTimeoutMs = 15000, pollMs = 200 } = opts;
  const emit = makeLineSplitter((line) => onLine(stripAnsi(line)));
  const aborted = () => signal?.aborted === true;
  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const t = setTimeout(resolve, ms);
      signal?.addEventListener(
        "abort",
        () => {
          clearTimeout(t);
          resolve();
        },
        { once: true },
      );
    });

  // 1. Wait for the child's logfile to appear. It's created at spawn, so this
  //    is a short wait — bounded so a start that never writes a log can't hang.
  const discoverDeadline = Date.now() + discoverTimeoutMs;
  let found: string | null = null;
  while (!aborted() && Date.now() < discoverDeadline) {
    found = findLiveServerLog(startedAtMs);
    if (found) break;
    // eslint-disable-next-line no-await-in-loop -- sequential poll for a file that appears at spawn
    await sleep(pollMs);
  }
  if (!found) return; // No logfile found — the caller still reports ready/fail.
  const logPath = found;

  // 2. Read from the start, then follow appended bytes. fs.watch can miss rapid
  //    writes, so a poll tick also drains until the caller aborts.
  let offset = 0;
  const readNew = () => {
    try {
      const size = fs.statSync(logPath).size;
      if (size <= offset) return;
      const fd = fs.openSync(logPath, "r");
      try {
        // readSync may return fewer bytes than requested (large append, pipe-
        // backed fs); loop until the range up to `size` is drained, advancing
        // `offset` only by bytes actually read so a short read can't emit
        // uninitialized buffer or skip content.
        const buf = Buffer.alloc(size - offset);
        let filled = 0;
        while (offset + filled < size) {
          const n = fs.readSync(fd, buf, filled, buf.length - filled, offset + filled);
          if (n <= 0) break; // EOF (file truncated since stat) — emit what we have
          filled += n;
        }
        if (filled > 0) {
          offset += filled;
          emit.push(buf.toString("utf8", 0, filled));
        }
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      // File rotated/removed mid-tail — stop reading; the caller drives the end.
    }
  };

  let watcher: fs.FSWatcher | null = null;
  try {
    watcher = fs.watch(logPath, { persistent: false }, readNew);
  } catch {
    // fs.watch unsupported here — the poll loop below still drains new bytes.
  }
  try {
    readNew();
    while (!aborted()) {
      // eslint-disable-next-line no-await-in-loop -- sequential poll: tick, drain, until aborted
      await sleep(pollMs);
      readNew();
    }
    readNew(); // final drain of any lines written just before the abort
  } finally {
    if (watcher) watcher.close();
  }
}

/** Stop the local background server (and its attached host daemon). */
export async function stopLocalServer(cliPath: CliCommand): Promise<CliCommandResult> {
  const res = await runCli(cliPath, ["server", "stop"], { timeoutMs: 15000 });
  return { ok: res.code === 0, output: (res.stdout || res.stderr).trim() };
}

/**
 * Tell the server to drop a host daemon it owns for this target. Used to
 * disconnect a daemon the desktop adopted rather than spawned.
 */
export async function stopHost(cliPath: CliCommand, serverUrl: string): Promise<CliCommandResult> {
  const res = await runCli(cliPath, ["host", "stop", "--server", serverUrl], {
    timeoutMs: 15000,
  });
  return { ok: res.code === 0, output: (res.stdout || res.stderr).trim() };
}

/** True when any recorded server URL matches, including loopback aliases.
 * Local-mode records may carry the URL only in resolved_server_url. */
export function matchesServer(daemon: unknown, serverUrl: unknown): boolean {
  if (!daemon || typeof daemon !== "object") return false;
  const want = normalizeServerUrl(serverUrl);
  if (want === "") return false;
  return [
    propertyOf(daemon, "server_url"),
    propertyOf(daemon, "target"),
    propertyOf(daemon, "resolved_server_url"),
  ].some((field) => normalizeServerUrl(field) === want || sameLoopbackServer(field, serverUrl));
}

/**
 * Directory holding per-target daemon registry records, mirroring
 * `_daemon_registry_dir()` in omnigent/cli.py (`<state_dir>/daemons`).
 */
export function daemonRegistryDir(): string {
  return path.join(stateDir(), "daemons");
}

/** The subset of a daemon registry record the desktop needs. */
export interface DaemonRecord {
  pid: number;
  target: string;
  mode: "local" | "server";
  server_url: string | null;
  resolved_server_url: string | null;
  host_id: string | null;
  log_path: string | null;
}

/**
 * Parse one decoded daemon registry record into the subset the desktop needs.
 * Mirrors the validation in `_record_from_json()` (omnigent/cli.py): a usable
 * record needs a positive integer `pid`, a non-empty `target`, and a known
 * `mode`. Returns null for malformed records.
 */
export function parseDaemonRecord(raw: unknown): DaemonRecord | null {
  if (!raw || typeof raw !== "object") return null;
  const rawPid = propertyOf(raw, "pid");
  const pid =
    typeof rawPid === "number"
      ? rawPid
      : typeof rawPid === "string"
        ? Number.parseInt(rawPid, 10)
        : NaN;
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const rawTarget = propertyOf(raw, "target");
  const target = typeof rawTarget === "string" ? rawTarget : "";
  const rawMode = propertyOf(raw, "mode");
  const mode = rawMode === "local" || rawMode === "server" ? rawMode : "";
  if (!target || !mode) return null;
  const str = (v: unknown) => (typeof v === "string" && v ? v : null);
  return {
    pid,
    target,
    mode,
    server_url: str(propertyOf(raw, "server_url")),
    resolved_server_url: str(propertyOf(raw, "resolved_server_url")),
    host_id: str(propertyOf(raw, "host_id")),
    log_path: str(propertyOf(raw, "log_path")),
  };
}

/**
 * Read every daemon registry record from disk (`~/.omnigent/daemons/*.json`).
 * This is the fast substitute for `omnigent host status --json`: it gives the
 * daemon metadata and (with a pid-liveness check) process state without the
 * per-session runner probes that make the CLI command slow. Tunnel health
 * ({@link probeHostTunnel}) is layered on separately. Returns [] when the
 * registry is absent.
 */
export function readDaemonRecords(): DaemonRecord[] {
  const dir = daemonRegistryDir();
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const records: DaemonRecord[] = [];
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
 */
export function daemonServerUrl(record: DaemonRecord | null | undefined): string | null {
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
 */
export function bearerTokenFor(serverUrl: string): string | null {
  if (typeof serverUrl !== "string" || serverUrl === "") return null;
  const key = serverUrl.replace(/\/+$/, "");
  let data: unknown;
  try {
    data = JSON.parse(fs.readFileSync(path.join(stateDir(), "auth_tokens.json"), "utf8"));
  } catch {
    return null;
  }
  const entry = data && typeof data === "object" ? propertyOf(data, key) : null;
  if (!entry || typeof entry !== "object") return null;
  const token = propertyOf(entry, "token");
  if (typeof token === "string" && token !== "") {
    const expiresAt = propertyOf(entry, "expires_at");
    if (typeof expiresAt === "number" && expiresAt < Date.now() / 1000) return null;
    return token;
  }
  return null;
}

const OMNIGENT_SLICE_KEY_HEADER = "X-Databricks-Omnigent-Slice-Key";
const WORKSPACE_API_PATH = "/api/2.0/omnigent";

/**
 * Whether requests to `serverUrl` should carry a host's routing key, by the same
 * rule as `databricks_request_headers()` (omnigent/cli_auth.py): on by default
 * for workspace mounts, opt-in on other servers with
 * `OMNIGENT_HOST_SLICE_KEY_ENABLED=1`, and off for either when it is "0".
 */
export function hostRoutingEnabled(serverUrl: string): boolean {
  const setting = process.env.OMNIGENT_HOST_SLICE_KEY_ENABLED;
  if (setting === "0") return false;
  if (setting === "1") return true;
  try {
    return new URL(serverUrl).pathname.replace(/\/+$/, "") === WORKSPACE_API_PATH;
  } catch {
    return false;
  }
}

/** A host tunnel's reported status, or why it could not be read. */
export interface HostTunnelProbe {
  status: string | null;
  reachable: boolean;
  authMissing: boolean;
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
 */
export async function probeHostTunnel(
  serverUrl: string,
  hostId: string | null,
  { timeoutMs = 2000 }: CliTimeoutOptions = {},
): Promise<HostTunnelProbe> {
  if (typeof serverUrl !== "string" || !serverUrl || typeof hostId !== "string" || !hostId) {
    return { status: null, reachable: false, authMissing: false };
  }
  const headers: Record<string, string> = {};
  // A loopback server is usually single-user (no auth), but send a stored token
  // if one happens to exist so an authed loopback server still verifies. A
  // remote server with no obtainable token can't be probed in-process (a
  // Databricks-pointer login mints tokens via the SDK) → report authMissing so
  // the caller falls back to the optimistic/unverified path, not "offline".
  const token = bearerTokenFor(serverUrl);
  if (token) headers.Authorization = `Bearer ${token}`;
  else if (!isLoopbackServer(serverUrl))
    return { status: null, reachable: false, authMissing: true };
  // Route to the replica holding this host's tunnel, the one whose status we want.
  if (hostRoutingEnabled(serverUrl)) headers[OMNIGENT_SLICE_KEY_HEADER] = hostId;
  const base = serverUrl.replace(/\/+$/, "");
  const target = `${base}/v1/hosts/${encodeURIComponent(hostId)}`;
  try {
    const resp = await fetch(target, { headers, signal: AbortSignal.timeout(timeoutMs) });
    if (!resp.ok) return { status: null, reachable: true, authMissing: false };
    const body: unknown = await resp.json().catch(() => null);
    const bodyStatus = body ? propertyOf(body, "status") : undefined;
    const status = body && typeof bodyStatus === "string" ? bodyStatus : null;
    return { status, reachable: true, authMissing: false };
  } catch {
    // Connection refused / unreachable / timed out → can't confirm the tunnel.
    return { status: null, reachable: false, authMissing: false };
  }
}

/** Whether `/v1/me` answered 200, and whether the server answered at all. */
export interface ServerAuthProbe {
  authed: boolean;
  reachable: boolean;
}

/**
 * Whether the CLI is authenticated to `serverUrl` right now, decided by the same
 * signal the Python side trusts to gate a connect: a single `GET
 * {serverUrl}/v1/me`. A 200 means authed; anything else (a 401, or a
 * Databricks-edge 302 to the workspace OAuth page) means a login is needed —
 * mirroring `_ensure_databricks_server_auth()` and the `login` command in
 * omnigent/cli.py, so the desktop and CLI agree on "is auth needed?".
 *
 * Sends a stored session token ({@link bearerTokenFor}) when one exists, so an
 * already-authed OIDC/accounts server answers 200 and we skip a needless
 * `omnigent login`. A Databricks-pointer login stores no in-process token (the
 * SDK mints one per request), so the probe is unauthenticated and returns
 * not-authed — which correctly defers to `omnigent login`, itself browserless
 * while the workspace grant is still live and refreshable.
 *
 * `redirect: "manual"` so a Databricks 302 is never followed into a 200 login
 * page — an opaque redirect surfaces as a non-200 status, which is the answer we
 * want. `reachable:false` (a thrown fetch) lets the caller skip a doomed login
 * and let the connect attempt raise its own, clearer error. Never throws.
 */
export async function probeServerAuth(
  serverUrl: string,
  { timeoutMs = 10000 }: CliTimeoutOptions = {},
): Promise<ServerAuthProbe> {
  if (typeof serverUrl !== "string" || serverUrl === "") {
    return { authed: false, reachable: false };
  }
  const headers: Record<string, string> = {};
  const token = bearerTokenFor(serverUrl);
  if (token) headers.Authorization = `Bearer ${token}`;
  const base = serverUrl.replace(/\/+$/, "");
  try {
    const resp = await fetch(`${base}/v1/me`, {
      headers,
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
    return { authed: resp.status === 200, reachable: true };
  } catch {
    // Unreachable / timed out — can't confirm auth. Report unreachable so the
    // caller lets the connect attempt surface its own error rather than forcing
    // a login that can't complete against a dead server.
    return { authed: false, reachable: false };
  }
}

export interface HostConnectionOptions extends CliTimeoutOptions {
  probe?: boolean | undefined;
}

/** This machine's host connection to a server. */
export interface HostConnection {
  connected: boolean;
  process: "online" | "offline";
  hostStatus: string | null;
  pid: number | null;
  error: string | null;
  verified: boolean;
}

/**
 * This machine's connection to `serverUrl`, resolved WITHOUT the slow `omnigent
 * host status` subprocess: daemon metadata + process state come from the
 * on-disk registry ({@link readDaemonRecords}), and tunnel health from one
 * basic request ({@link probeHostTunnel}). Returns `{ connected, process,
 * hostStatus, pid, error }` plus `verified` (false when the tunnel couldn't be
 * probed — e.g. Databricks-pointer auth — so process-alive is reported
 * optimistically rather than as offline).
 */
export async function getHostConnectionFast(
  serverUrl: string,
  { probe = true, timeoutMs = 2000 }: HostConnectionOptions = {},
): Promise<HostConnection> {
  const match = liveDaemonRecordFor(serverUrl);
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

/** Find a live daemon enrolled with this server, including loopback URL aliases. */
export function liveDaemonRecordFor(serverUrl: string): DaemonRecord | null {
  return (
    readDaemonRecords().find(
      (record) => isPidAlive(record.pid) && matchesServer(record, serverUrl),
    ) ?? null
  );
}
