/**
 * Arca host connection.
 *
 * Connecting an Arca instance as an Omnigent host means running, over
 * `arca ssh`:
 *
 *   isaac omni host --server <url> --background --non-interactive
 *
 * The remote daemon then opens the ordinary outbound host tunnel using the
 * instance's own Omnigent OAuth grant; generic Arca sign-in is not sufficient.
 * `--background` exits 0 only once the daemon registered with the server, and
 * `--non-interactive` fails instead of waiting on a browser login, so the exit
 * code is a reliable signal.
 *
 * This module is main-process-free: the binary probe and process spawn are
 * injected so everything is unit-testable without Electron or a real arca.
 */

import { execFile, execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";

import {
  readArcaIdentity,
  IDENTITY_START,
  IDENTITY_END,
  type ArcaIdentity,
} from "./arcaIdentity.ts";
import type { ArcaExtendMode, DbcertRefreshResult, DbcertStatusResult } from "./ipc/bridge.ts";

/**
 * Connecting may cold-start the EC2 instance, which takes minutes — give the
 * whole ssh + remote daemon startup a generous ceiling.
 */
export const CONNECT_TIMEOUT_MS = 5 * 60 * 1000;
export const STATUS_TIMEOUT_MS = 60_000;
export const EXTEND_TIMEOUT_MS = 120_000;
/** A dbcert refresh waits for the user to finish its browser sign-in. */
export const DBCERT_REFRESH_TIMEOUT_MS = 5 * 60 * 1000;
const DBCERT_STATUS_TIMEOUT_MS = 10_000;
const DBCERT_ARCA_SYNC_TIMEOUT_MS = 60_000;
export const ARCA_EXTEND_MODES: readonly ArcaExtendMode[] = Object.freeze([
  "overnight",
  "workweek",
]);
const ARCA_AUTH_RE = /arca (auth )?login|certificate.*expired|permission denied \(publickey/i;
const MAX_CLI_OUTPUT_BYTES = 256 * 1024;

/**
 * The only characters allowed in the server URL that rides inside the ssh
 * remote command. ssh joins argv with spaces and the REMOTE shell re-parses
 * the line, so the URL (the one non-literal argument) must not smuggle shell
 * metacharacters (`;`, `$`, backticks, quotes, spaces…) through its path or
 * query — URL-legal but shell-hostile. Allowlist, not escape: a URL outside
 * this set is refused outright.
 */
export const SAFE_URL_RE = /^[A-Za-z0-9\-._~:/?=&%]+$/;

export type ArcaErrorKind =
  | "timeout"
  | "omni-auth"
  | "arca-auth"
  | "missing-remote-cli"
  | "unreachable"
  | "not-installed"
  | "no-instance"
  | "runtime-limit"
  | "unknown";

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** `error.message` as plain property access reads it: a thrown null or undefined throws here. */
function messageOf(error: unknown): unknown {
  return (error as { message?: unknown }).message;
}

/** A child's exit code as Node reports it on `exit` and `close`. */
function exitCodeOf(value: unknown): number | null {
  // Node passes a number, or null after a signal; the cast keeps the value as is.
  return value as number | null;
}

/**
 * Well-known install locations for the arca binary. Probed because a
 * GUI-launched Electron app inherits a minimal PATH (mirrors the omnigent CLI
 * resolution in omnigent_cli.ts).
 */
function candidatePaths(): string[] {
  const home = os.homedir();
  return [
    "/usr/local/bin/arca",
    "/opt/homebrew/bin/arca",
    path.join(home, ".local", "bin", "arca"),
  ];
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
 * Resolve `arca` on PATH via the shell (so login-shell PATHs resolve), else
 * null. Arca is macOS-only, so no Windows branch.
 */
function whichArca(): string | null {
  try {
    const out = execFileSync("/bin/sh", ["-c", "command -v arca"], { encoding: "utf8" });
    return out.trim() || null;
  } catch {
    return null;
  }
}

/** Probes {@link resolveArcaPath} uses; each defaults to the real one. */
export interface ResolveArcaPathDeps {
  isExecutableFile?: ((p: string) => boolean) | undefined;
  whichArca?: (() => string | null) | undefined;
  candidatePaths?: (() => string[]) | undefined;
}

/** Probes {@link resolveArcaPathAsync} uses; PATH is always probed through the shell. */
export interface ResolveArcaPathAsyncDeps {
  isExecutableFile?: ((p: string) => boolean) | undefined;
  candidatePaths?: (() => string[]) | undefined;
}

/**
 * Locate the arca binary: PATH first, then well-known locations. Null when
 * arca isn't installed on this machine.
 */
export function resolveArcaPath(deps: ResolveArcaPathDeps = {}): string | null {
  const isExec = deps.isExecutableFile || isExecutableFile;
  const onPath = (deps.whichArca || whichArca)();
  if (onPath && isExec(onPath)) return onPath;
  for (const candidate of (deps.candidatePaths || candidatePaths)()) {
    if (isExec(candidate)) return candidate;
  }
  return null;
}

/**
 * {@link resolveArcaPath} without blocking the caller: the PATH probe runs the
 * shell asynchronously, so the Electron main process never stalls on it.
 */
export function resolveArcaPathAsync(deps: ResolveArcaPathAsyncDeps = {}): Promise<string | null> {
  return new Promise((resolve) => {
    execFile("/bin/sh", ["-c", "command -v arca"], { encoding: "utf8" }, (error, stdout) => {
      const onPath = error ? null : String(stdout).trim() || null;
      resolve(resolveArcaPath({ ...deps, whichArca: () => onPath }));
    });
  });
}

/**
 * Build the arca argv that connects the instance to `serverUrl`. Everything
 * after "ssh" is passed through to ssh and runs as the remote command.
 *
 * @param login Prepare the remote grant instead of starting a host.
 */
function buildArcaArgs(serverUrl: string, login = false): string[] {
  const url = new URL(serverUrl);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error(`unsupported server URL scheme: ${url.protocol}`);
  }
  if (!SAFE_URL_RE.test(url.toString())) {
    throw new Error("server URL contains characters that are not allowed in an ssh command");
  }
  return [
    "ssh",
    // A headless launch needs no port forwards. Clear the reserved -R 19222
    // from ~/.ssh/config so a listener reclaimed during cold start can't kill
    // the session after the remote command succeeds.
    "-o",
    "ClearAllForwardings=yes",
    "isaac",
    "omni",
    // Quoted for the remote shell, which would glob a `?` (zsh fails on no
    // match); SAFE_URL_RE already bars `'`, so the quotes can't be broken out of.
    ...(login
      ? ["login", `'${url.toString()}'`]
      : [
          "host",
          "--server",
          `'${url.toString()}'`,
          "--background",
          "--non-interactive",
          "&&",
          "{",
          "printf",
          `'\\n${IDENTITY_START}\\n'`,
          ";",
          "isaac",
          "omni",
          "host",
          "status",
          "--server",
          `'${url.toString()}'`,
          "--json",
          ";",
          "printf",
          `'\\n${IDENTITY_END}\\n'`,
          ";",
          "}",
        ]),
  ];
}

export function buildConnectArgs(serverUrl: string): string[] {
  return buildArcaArgs(serverUrl);
}

export function buildLoginArgs(serverUrl: string): string[] {
  return buildArcaArgs(serverUrl, true);
}

/** A finished arca run's exit code and captured output. */
export interface ArcaRunOutput {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut?: boolean | undefined;
}

/** A failed connect, worded for the user. */
export interface ArcaConnectFailure {
  ok: false;
  error: string;
  errorMessage?: string;
  errorAction?: string;
  errorKind: ArcaErrorKind;
  authError?: boolean;
}

/**
 * Map a failed connect run to an actionable user-facing result. Matched
 * against known arca / omnigent CLI failure shapes; anything unrecognized
 * falls through to the captured output.
 *
 * `errorKind` lets callers offer the one fix that applies (retry, sign in on
 * Arca, or `arca login` on this machine).
 */
export function describeConnectFailure(run: ArcaRunOutput): ArcaConnectFailure {
  const output = `${run.stderr}\n${run.stdout}`;
  if (run.timedOut) {
    return {
      ok: false,
      errorKind: "timeout",
      error:
        "Connecting to Arca timed out. The instance may still be starting — " +
        "check `arca status` and try again.",
      errorMessage: "Connecting to Arca timed out. The instance may still be starting.",
      errorAction: "Check `arca status` and try again.",
    };
  }
  // `omni host --non-interactive` fails with a sign-in hint when the instance's
  // credentials can't mint a server token.
  if (/OMNIGENT_AUTH_REQUIRED|not signed in|authentication failed \(HTTP 401\)/i.test(output)) {
    return {
      ok: false,
      authError: true,
      errorKind: "omni-auth",
      error:
        "The Arca instance isn't signed in to this server. Run " +
        "`arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
      errorMessage: "The Arca instance isn't signed in to this server.",
      errorAction:
        "Run `arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
    };
  }
  // The remote shell couldn't find isaac (or isaac couldn't find omni) on the
  // Arca instance.
  if (run.code === 127 || /(isaac|omni(gent)?):? .*(command )?not found/i.test(output)) {
    return {
      ok: false,
      errorKind: "missing-remote-cli",
      error:
        "`isaac omni` isn't available on the Arca instance. " +
        "Check the isaac setup there (`arca ssh`, then `isaac omni --help`) and try again.",
      errorMessage: "`isaac omni` isn't available on the Arca instance.",
      errorAction:
        "Check the isaac setup there (`arca ssh`, then `isaac omni --help`) and try again.",
    };
  }
  // This machine's arca credentials are missing or expired, so ssh never
  // reached the instance.
  if (ARCA_AUTH_RE.test(output)) {
    return {
      ok: false,
      errorKind: "arca-auth",
      error:
        "Your arca sign-in on this machine has expired. Run `arca login` in a terminal, then try again.",
      errorMessage: "Your arca sign-in on this machine has expired.",
      errorAction: "Run `arca login` in a terminal, then try again.",
    };
  }
  if (/error connecting to arca/i.test(output)) {
    return {
      ok: false,
      errorKind: "unreachable",
      error:
        "Couldn't reach the Arca instance. Try `arca stop && arca start` in a terminal, " +
        "then connect again.",
      errorMessage: "Couldn't reach the Arca instance.",
      errorAction: "Try `arca stop && arca start` in a terminal, then connect again.",
    };
  }
  const detail = run.stderr.trim() || run.stdout.trim();
  return {
    ok: false,
    errorKind: "unknown",
    error: detail
      ? `Connecting to Arca failed: ${lastLine(detail)}`
      : `Connecting to Arca failed (exit code ${run.code ?? "unknown"}).`,
  };
}

/**
 * The last non-empty line of captured output — arca and ssh are chatty, and
 * the final line is where both put the actual error.
 */
export function lastLine(text: string): string {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return lines[lines.length - 1] ?? text;
}

/** This machine's Arca instance state, as `arca status --json` reports it. */
export interface ArcaStatus {
  state: string | null;
  hasInstance: boolean;
  shutdownAt: number | null;
  rawShutdownTime: string | null;
}

/** Parse the status object even when the CLI surrounds it with notices. */
export function parseArcaStatus(stdout: string): ArcaStatus | null {
  const isStatus = (value: Record<string, unknown>) => {
    const hasInstance =
      Object.hasOwn(value, "instance") &&
      (value.instance === null ||
        (typeof value.instance === "object" && !Array.isArray(value.instance)));
    return (
      typeof value.status === "string" && (hasInstance || Object.hasOwn(value, "shutdown_time"))
    );
  };
  const parseObject = (text: string): Record<string, unknown> | null => {
    try {
      const value: unknown = JSON.parse(text);
      return value && typeof value === "object" && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : null;
    } catch {
      return null;
    }
  };
  let status = parseObject(stdout.trim());
  if (status && !isStatus(status)) status = null;
  if (!status) {
    let start = -1;
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let i = 0; i < stdout.length; i++) {
      const char = stdout[i];
      if (depth === 0) {
        if (char === "{") {
          start = i;
          depth = 1;
        }
        continue;
      }
      if (inString) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inString = false;
      } else if (char === '"') {
        inString = true;
      } else if (char === "{") {
        depth++;
      } else if (char === "}" && --depth === 0) {
        const candidate = parseObject(stdout.slice(start, i + 1));
        if (candidate && isStatus(candidate)) {
          if (Object.hasOwn(candidate, "instance")) {
            status = candidate;
            break;
          }
          status ??= candidate;
        }
      }
    }
  }
  if (!status) return null;
  const raw =
    status.instance === null || status.instance === undefined
      ? status.shutdown_time
      : propertyOf(status.instance, "shutdown_time");
  const rawShutdownTime = typeof raw === "string" ? raw : null;
  const shutdownAt =
    rawShutdownTime && /(?:Z|[+-]\d{2}:?\d{2})$/i.test(rawShutdownTime)
      ? Date.parse(rawShutdownTime.replace(/([+-]\d{2})(\d{2})$/, "$1:$2"))
      : NaN;
  return {
    state: typeof status.status === "string" ? status.status : null,
    hasInstance:
      status.instance !== null &&
      typeof status.instance === "object" &&
      !Array.isArray(status.instance),
    shutdownAt: Number.isFinite(shutdownAt) ? shutdownAt : null,
    rawShutdownTime,
  };
}

/** An arca or dbcert child's output stream. */
export interface ArcaChildOutput {
  on: (event: "data", listener: (chunk: unknown) => void) => unknown;
  off: (event: "data", listener: (chunk: unknown) => void) => unknown;
  destroy?: (() => unknown) | undefined;
}

/** The part of a spawned arca or dbcert `ChildProcess` this module uses. */
export interface ArcaChild {
  pid?: number | undefined;
  stdout?: ArcaChildOutput | null | undefined;
  stderr?: ArcaChildOutput | null | undefined;
  kill: (signal?: NodeJS.Signals) => unknown;
  // Node passes an `Error` for "error", and the exit code for "exit" and "close".
  on: (event: "error" | "exit" | "close", listener: (value: unknown) => void) => unknown;
}

export interface ArcaSpawnOptions {
  stdio: ["ignore", "pipe", "pipe"];
  detached?: boolean;
  env?: NodeJS.ProcessEnv;
}

/** `child_process.spawn`, injectable so tests need no real arca or dbcert. */
export type SpawnArca = (command: string, args: string[], options: ArcaSpawnOptions) => ArcaChild;

interface RunArcaOptions {
  spawn?: SpawnArca | undefined;
  timeoutMs: number;
  killGraceMs?: number | undefined;
  maxOutputBytes?: number | undefined;
  env?: NodeJS.ProcessEnv | undefined;
}

interface RunArcaResult extends ArcaRunOutput {
  /** What spawn threw, or what the child's "error" event carried. */
  spawnError?: unknown;
}

/** Run a headless Arca command and capture both output streams. */
function runArca(
  arcaPath: string,
  args: string[],
  options: RunArcaOptions,
): Promise<RunArcaResult> {
  return new Promise((resolve) => {
    let child: ArcaChild;
    try {
      child = (options.spawn || spawn)(arcaPath, args, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
        ...(options.env ? { env: options.env } : {}),
      });
    } catch (error) {
      resolve({ code: null, stdout: "", stderr: "", spawnError: error });
      return;
    }
    let stdout = "";
    let stdoutBytes = 0;
    let stderr = "";
    let stderrBytes = 0;
    const stdoutDecoder = new StringDecoder("utf8");
    const stderrDecoder = new StringDecoder("utf8");
    let settled = false;
    let exited = false;
    let exitCode: number | null = null;
    let exitGraceTimer: ReturnType<typeof setTimeout> | undefined;
    let killTimer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    const killTree = (signal: NodeJS.Signals) => {
      try {
        if (typeof child.pid === "number") process.kill(-child.pid, signal);
        else child.kill(signal);
      } catch {
        try {
          child.kill(signal);
        } catch {
          // Already gone.
        }
      }
    };
    const onStdout = (chunk: unknown) => {
      if (stdoutBytes >= (options.maxOutputBytes ?? Infinity)) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
      const retained = bytes.subarray(0, (options.maxOutputBytes ?? Infinity) - stdoutBytes);
      stdout += stdoutDecoder.write(retained);
      stdoutBytes += retained.length;
    };
    const onStderr = (chunk: unknown) => {
      if (stderrBytes >= (options.maxOutputBytes ?? Infinity)) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
      const retained = bytes.subarray(0, (options.maxOutputBytes ?? Infinity) - stderrBytes);
      stderr += stderrDecoder.write(retained);
      stderrBytes += retained.length;
    };
    const settle = (
      result: { code: number | null; timedOut?: boolean; spawnError?: unknown },
      fromClose = false,
    ) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(exitGraceTimer);
      clearTimeout(killTimer);
      stdout += stdoutDecoder.end();
      stderr += stderrDecoder.end();
      if (!fromClose) {
        const streams: [ArcaChildOutput | null | undefined, (chunk: unknown) => void][] = [
          [child.stdout, onStdout],
          [child.stderr, onStderr],
        ];
        for (const [stream, listener] of streams) {
          try {
            stream?.off("data", listener);
            stream?.destroy?.();
          } catch {
            // A child may already have closed the pipe.
          }
        }
      }
      const captured = { code: result.code, stdout, stderr, timedOut };
      resolve({ ...captured, ...result });
    };
    const timer = setTimeout(() => {
      if (exited) {
        settle({ code: exitCode });
        return;
      }
      timedOut = true;
      killTree("SIGTERM");
      killTimer = setTimeout(() => {
        killTree("SIGKILL");
        settle({ code: null, timedOut: true });
      }, options.killGraceMs ?? 5000);
    }, options.timeoutMs);
    child.stdout?.on("data", onStdout);
    child.stderr?.on("data", onStderr);
    child.on("error", (error) => settle({ code: null, spawnError: error }));
    child.on("exit", (code) => {
      if (settled) return;
      exited = true;
      exitCode = exitCodeOf(code);
      exitGraceTimer = setTimeout(() => settle({ code: exitCodeOf(code) }), 500);
    });
    child.on("close", (code) => settle({ code: exited ? exitCode : exitCodeOf(code) }, true));
  });
}

/** A failed status or extend run, worded for the user. */
export interface ArcaCliFailure {
  ok: false;
  errorKind: ArcaErrorKind;
  error: string;
}

/** Turn a failed status or extend run into an actionable result. */
function describeArcaCliFailure(run: ArcaRunOutput, action: "status" | "extend"): ArcaCliFailure {
  const output = `${run.stdout}\n${run.stderr}`;
  if (run.timedOut) {
    return {
      ok: false,
      errorKind: "timeout",
      error:
        action === "status"
          ? "Checking Arca's status timed out. Try again."
          : "Extending Arca timed out. Try again.",
    };
  }
  if (/no running arca instance/i.test(output)) {
    return {
      ok: false,
      errorKind: "no-instance",
      error:
        action === "status"
          ? "Your Arca instance isn't running. Start it with `arca start` and try again."
          : "Your Arca instance isn't running anymore, so there's nothing to extend.",
    };
  }
  if (/runtime limit/i.test(output)) {
    return {
      ok: false,
      errorKind: "runtime-limit",
      error:
        action === "status"
          ? "Your Arca instance reached its one-week runtime limit. Restart it with `arca stop && arca start`, then check its status again."
          : "Your Arca instance reached its one-week runtime limit, so it can't be extended. Restart it with `arca stop && arca start`.",
    };
  }
  if (ARCA_AUTH_RE.test(output) || /dbcert/i.test(output)) {
    return {
      ok: false,
      errorKind: "arca-auth",
      error:
        "Your arca sign-in on this machine has expired. Run `arca login` in a terminal, then try again.",
    };
  }
  if (/error connecting to arca/i.test(output)) {
    return {
      ok: false,
      errorKind: "unreachable",
      error:
        "Couldn't reach the Arca instance. Try `arca stop && arca start` in a terminal, then try again.",
    };
  }
  const detail = lastLine(output.trim());
  return {
    ok: false,
    errorKind: "unknown",
    error: detail
      ? `Arca ${action} failed: ${detail}`
      : `Arca ${action} failed (exit code ${run.code ?? "unknown"}).`,
  };
}

/** What the status and extend commands use; each defaults to the real one. */
export interface ArcaCliDeps {
  resolveArcaPath?: (() => string | null | Promise<string | null>) | undefined;
  spawn?: SpawnArca | undefined;
  timeoutMs?: number | undefined;
  killGraceMs?: number | undefined;
}

export interface ArcaStatusSuccess extends ArcaStatus {
  ok: true;
}

export type ArcaStatusResult = ArcaStatusSuccess | ArcaCliFailure;

export type ArcaExtendOutcome = { ok: true; shutdownAt: number | null } | ArcaCliFailure;

/** Read this machine's Arca instance state and shutdown time. */
export async function readArcaStatus(deps: ArcaCliDeps = {}): Promise<ArcaStatusResult> {
  try {
    const arcaPath = await (deps.resolveArcaPath || resolveArcaPathAsync)();
    if (!arcaPath)
      return {
        ok: false,
        errorKind: "not-installed",
        error: "The arca CLI was not found on this machine.",
      };
    const run = await runArca(arcaPath, ["status", "--json"], {
      spawn: deps.spawn,
      timeoutMs: deps.timeoutMs ?? STATUS_TIMEOUT_MS,
      killGraceMs: deps.killGraceMs,
      maxOutputBytes: MAX_CLI_OUTPUT_BYTES,
    });
    if (run.spawnError)
      return {
        ok: false,
        errorKind: "unknown",
        error: `Couldn't run arca: ${messageOf(run.spawnError)}`,
      };
    if (run.code !== 0 || run.timedOut) return describeArcaCliFailure(run, "status");
    const parsed = parseArcaStatus(run.stdout);
    if (!parsed)
      return {
        ok: false,
        errorKind: "unknown",
        error:
          "Unexpected arca status output. Run `arca status --json` in a terminal and try again.",
      };
    return { ok: true, ...parsed };
  } catch (error) {
    return { ok: false, errorKind: "unknown", error: `Couldn't run arca: ${messageOf(error)}` };
  }
}

/** Whether `mode` is an approved extend mode; any value may arrive from the renderer. */
function isArcaExtendMode(mode: unknown): mode is ArcaExtendMode {
  return (ARCA_EXTEND_MODES as readonly unknown[]).includes(mode);
}

/** Extend this machine's Arca instance using an approved mode. */
export async function runArcaExtend(
  mode: unknown,
  deps: ArcaCliDeps = {},
): Promise<ArcaExtendOutcome> {
  if (!isArcaExtendMode(mode))
    return { ok: false, errorKind: "unknown", error: "Unsupported arca extend mode." };
  try {
    const arcaPath = await (deps.resolveArcaPath || resolveArcaPathAsync)();
    if (!arcaPath)
      return {
        ok: false,
        errorKind: "not-installed",
        error: "The arca CLI was not found on this machine.",
      };
    const status = await readArcaStatus({ ...deps, resolveArcaPath: () => arcaPath });
    if (!status.ok) return status;
    if (status.state?.toLowerCase() !== "running" || !status.hasInstance) {
      return {
        ok: false,
        errorKind: "no-instance",
        error: "Your Arca instance is not running. Start it and try again.",
      };
    }
    const run = await runArca(arcaPath, ["extend", mode], {
      spawn: deps.spawn,
      timeoutMs: deps.timeoutMs ?? EXTEND_TIMEOUT_MS,
      killGraceMs: deps.killGraceMs,
      maxOutputBytes: MAX_CLI_OUTPUT_BYTES,
    });
    if (run.spawnError)
      return {
        ok: false,
        errorKind: "unknown",
        error: `Couldn't run arca: ${messageOf(run.spawnError)}`,
      };
    if (run.code !== 0 || run.timedOut) return describeArcaCliFailure(run, "extend");
    const updated = await readArcaStatus({ ...deps, resolveArcaPath: () => arcaPath });
    if (!updated.ok) console.warn("[arca] status verification after extend failed:", updated.error);
    return { ok: true, shutdownAt: updated.ok ? updated.shutdownAt : null };
  } catch (error) {
    return { ok: false, errorKind: "unknown", error: `Couldn't run arca: ${messageOf(error)}` };
  }
}

/** What a streamed connect or login uses; each defaults to the real one. */
export interface StartArcaCommandDeps {
  timeoutMs?: number | undefined;
  resolveArcaPath?: (() => string | null) | undefined;
  spawn?: SpawnArca | undefined;
  onOutput?: ((text: string) => void) | undefined;
  onIdentityUnavailable?: ((reason: string) => void) | undefined;
}

export interface ArcaCommandSuccess {
  ok: true;
  alreadyRunning: boolean;
  identity?: ArcaIdentity;
}

export interface ArcaCommandFailure {
  ok: false;
  error: string;
  errorKind?: ArcaErrorKind;
  errorMessage?: string;
  errorAction?: string;
  authError?: boolean;
  canceled?: boolean;
}

export type ArcaCommandResult = ArcaCommandSuccess | ArcaCommandFailure;

/** A started connect or login; `command` is null when nothing was started. */
export interface ArcaCommand {
  command: string | null;
  promise: Promise<ArcaCommandResult>;
  cancel: () => void;
}

/**
 * Start connecting the user's Arca instance to `serverUrl` as an Omnigent
 * host, streaming the command's live output. Built for the connect console:
 * the caller shows `command` to the user, pipes `onOutput` chunks into a
 * terminal pane, and may `cancel()` (window closed). The promise never
 * rejects — every failure resolves as `{ ok: false, error }`.
 *
 * @param serverUrl The window's connected server URL.
 */
function startArcaCommand(
  serverUrl: string,
  deps: StartArcaCommandDeps = {},
  login = false,
): ArcaCommand {
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
  let args: string[];
  try {
    args = buildArcaArgs(serverUrl, login);
  } catch (error) {
    return {
      command: null,
      promise: Promise.resolve({ ok: false, error: `Invalid server URL: ${messageOf(error)}` }),
      cancel: () => {},
    };
  }
  const spawnFn: SpawnArca = deps.spawn || spawn;
  let child: ArcaChild;
  try {
    child = spawnFn(arcaPath, args, { stdio: ["ignore", "pipe", "pipe"] });
  } catch {
    return {
      command: `arca ${args.join(" ")}`,
      promise: Promise.resolve({ ok: false, error: "Couldn't start the Arca command." }),
      cancel: () => {},
    };
  }
  let stdout = "";
  let stderr = "";
  // Assigned by the executor below, which runs synchronously.
  let settle!: (result: ArcaCommandResult) => void;
  const promise = new Promise<ArcaCommandResult>((resolve) => {
    const timer = setTimeout(() => {
      settle(
        login
          ? {
              ok: false,
              errorKind: "timeout",
              error:
                "Arca sign-in timed out. Check Arca Companion, finish browser sign-in, and try again.",
            }
          : describeConnectFailure({ code: null, stdout: "", stderr: "", timedOut: true }),
      );
      try {
        child.kill();
      } catch {
        // Already gone.
      }
    }, timeoutMs);
    if (typeof timer.unref === "function") timer.unref();
    child.stdout?.on("data", (chunk) => {
      const text = String(chunk);
      if (!login) {
        stdout = (stdout + text).slice(-8000);
        onOutput(text);
      }
    });
    child.stderr?.on("data", (chunk) => {
      const text = String(chunk);
      // Login output can contain an OAuth ticket; keep it out of every renderer.
      if (!login) {
        stderr = (stderr + text).slice(-8000);
        onOutput(text);
      }
    });
    let settled = false;
    settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    child.on("error", (error) => {
      settle({ ok: false, error: `Couldn't run arca: ${messageOf(error)}` });
    });
    child.on("exit", (code) => {
      if (code === 0) {
        // `omni host --background` reuses a healthy daemon and says so — the
        // caller can then skip waiting for a host that was online all along.
        const identity = login
          ? null
          : readArcaIdentity(stdout, serverUrl, deps.onIdentityUnavailable);
        settle({
          ok: true,
          alreadyRunning: /already running/i.test(stdout + stderr),
          ...(identity ? { identity } : {}),
        });
        return;
      }
      if (login) {
        if (code === 127) {
          settle(describeConnectFailure({ code, stdout: "", stderr: "" }));
          return;
        }
        if (code === 255) {
          settle({
            ok: false,
            errorKind: "unreachable",
            error:
              "Arca sign-in couldn't reach the remote command. Check `arca ssh` in a terminal and try again.",
          });
          return;
        }
        settle({
          ok: false,
          authError: true,
          errorKind: "omni-auth",
          error:
            "Arca sign-in didn't complete. Check Arca Companion, finish browser sign-in, and try again.",
        });
        return;
      }
      settle(describeConnectFailure({ code: exitCodeOf(code), stdout, stderr }));
    });
  });
  return {
    command: `arca ${args.join(" ")}`,
    promise,
    cancel: () => {
      settle({
        ok: false,
        canceled: true,
        error: login ? "Arca sign-in was canceled." : "Connecting to Arca was canceled.",
      });
      try {
        child.kill();
      } catch {
        // Already gone.
      }
    },
  };
}

export function startArcaConnect(serverUrl: string, deps: StartArcaCommandDeps = {}): ArcaCommand {
  return startArcaCommand(serverUrl, deps);
}

export function startArcaLogin(serverUrl: string, deps: StartArcaCommandDeps = {}): ArcaCommand {
  return startArcaCommand(serverUrl, deps, true);
}

/**
 * Connect the user's Arca instance to `serverUrl` as an Omnigent host. Thin
 * non-streaming wrapper over {@link startArcaConnect}; never rejects.
 *
 * @param serverUrl The window's connected server URL.
 */
export function connectArcaHost(
  serverUrl: string,
  deps: StartArcaCommandDeps = {},
): Promise<ArcaCommandResult> {
  return startArcaConnect(serverUrl, deps).promise;
}

/** Well-known install locations for dbcert. */
function dbcertCandidatePaths(): string[] {
  return [
    "/usr/local/bin/dbcert",
    "/opt/homebrew/bin/dbcert",
    path.join(os.homedir(), ".local", "bin", "dbcert"),
  ];
}

/** Locate dbcert the way {@link resolveArcaPathAsync} locates arca. */
function resolveDbcertPathAsync(
  deps: Pick<ResolveArcaPathDeps, "isExecutableFile"> = {},
): Promise<string | null> {
  return new Promise((resolve) => {
    execFile("/bin/sh", ["-c", "command -v dbcert"], { encoding: "utf8" }, (error, stdout) => {
      const onPath = error ? null : String(stdout).trim() || null;
      resolve(
        resolveArcaPath({ ...deps, whichArca: () => onPath, candidatePaths: dbcertCandidatePaths }),
      );
    });
  });
}

/** What the dbcert commands use; each defaults to the real one. */
export interface DbcertDeps {
  resolveDbcertPath?: (() => string | null | Promise<string | null>) | undefined;
  spawn?: SpawnArca | undefined;
  timeoutMs?: number | undefined;
  killGraceMs?: number | undefined;
  skipArcaSync?: boolean | undefined;
}

/**
 * Renew this machine's dbcert with `dbcert --force`, which signs in through
 * the browser. Unless skipped, dbcert also syncs the primary Arca instance.
 * `--force` mints a new certificate even while the current one is valid.
 */
export async function runDbcertRefresh(deps: DbcertDeps = {}): Promise<DbcertRefreshResult> {
  return runDbcertCommand(
    ["--force", "--caller-name", "omnigent", "--update-kubeconfig=false"],
    "sign-in",
    deps,
  );
}

export async function runDbcertArcaSync(deps: DbcertDeps = {}): Promise<DbcertRefreshResult> {
  return runDbcertCommand(["sync-arca"], "Arca sync", {
    ...deps,
    timeoutMs: deps.timeoutMs ?? DBCERT_ARCA_SYNC_TIMEOUT_MS,
  });
}

async function runDbcertCommand(
  args: string[],
  action: string,
  deps: DbcertDeps,
): Promise<DbcertRefreshResult> {
  try {
    const dbcertPath = await (deps.resolveDbcertPath || resolveDbcertPathAsync)();
    if (!dbcertPath) return { ok: false, error: "dbcert was not found on this machine." };
    const run = await runArca(dbcertPath, args, {
      spawn: deps.spawn,
      timeoutMs: deps.timeoutMs ?? DBCERT_REFRESH_TIMEOUT_MS,
      killGraceMs: deps.killGraceMs,
      maxOutputBytes: MAX_CLI_OUTPUT_BYTES,
      ...(deps.skipArcaSync ? { env: { ...process.env, DBCERT_SKIP_ARCA_SYNC: "1" } } : {}),
    });
    if (run.spawnError)
      return { ok: false, error: `Couldn't run dbcert: ${messageOf(run.spawnError)}` };
    if (run.timedOut) return { ok: false, error: `The dbcert ${action} timed out.` };
    if (run.code !== 0) {
      const output = run.stderr.trim() || run.stdout.trim();
      return {
        ok: false,
        error: output ? lastLine(output) : `dbcert exited with code ${run.code}.`,
      };
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: `Couldn't run dbcert: ${messageOf(error)}` };
  }
}

/** Check both SSH and X.509 credentials without starting an interactive login. */
async function readDbcertStatus(dbcertPath: string, deps: DbcertDeps): Promise<DbcertStatusResult> {
  const run = await runArca(dbcertPath, ["status"], {
    spawn: deps.spawn,
    timeoutMs: deps.timeoutMs ?? DBCERT_STATUS_TIMEOUT_MS,
    killGraceMs: deps.killGraceMs,
    maxOutputBytes: 0,
  });
  if (run.spawnError || run.code === null || run.timedOut) {
    return {
      ok: false,
      error: "Couldn't check your laptop's dbcert. Try again, or check dbcert in Arca Companion.",
    };
  }
  if (run.code !== 0 && run.code !== 1) {
    return { ok: false, error: `Couldn't check your laptop's dbcert (exit ${run.code}).` };
  }
  return { ok: true, valid: run.code === 0 };
}

/** Check local credentials before provisioning an Arclet without starting sign-in. */
export async function checkDbcert(deps: DbcertDeps = {}): Promise<DbcertStatusResult> {
  try {
    const dbcertPath = await (deps.resolveDbcertPath || resolveDbcertPathAsync)();
    if (!dbcertPath) {
      return {
        ok: false,
        error: "dbcert is not installed on this laptop. Install dbcert, then try again.",
      };
    }
    return await readDbcertStatus(dbcertPath, deps);
  } catch {
    return { ok: false, error: "Couldn't check your laptop's dbcert. Try again." };
  }
}
