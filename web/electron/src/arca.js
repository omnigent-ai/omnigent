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
 * Arca box's own Omnigent OAuth grant; generic Arca sign-in is not sufficient.
 * `--background` exits 0 only once the daemon registered with the server,
 * and `--non-interactive` fails loud instead of dangling on a browser
 * login — both are what make the exit code a trustworthy signal here.
 *
 * This module is main-process-free: the binary probe and process spawn are
 * injected so everything is unit-testable without Electron or a real arca.
 */

const { execFile, execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { StringDecoder } = require("node:string_decoder");
const { readArcaIdentity, IDENTITY_START, IDENTITY_END } = require("./arcaIdentity");

/**
 * Connecting may cold-start the EC2 instance, which takes minutes — give the
 * whole ssh + remote daemon startup a generous ceiling.
 */
const CONNECT_TIMEOUT_MS = 5 * 60 * 1000;
const STATUS_TIMEOUT_MS = 60_000;
const EXTEND_TIMEOUT_MS = 120_000;
const ARCA_EXTEND_MODES = Object.freeze(["overnight", "workweek"]);
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
const SAFE_URL_RE = /^[A-Za-z0-9\-._~:/?=&%]+$/;

/**
 * @typedef {"timeout" | "omni-auth" | "arca-auth" | "missing-remote-cli" | "unreachable" | "not-installed" | "no-instance" | "runtime-limit" | "unknown"} ArcaErrorKind
 */

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
 * {@link resolveArcaPath} without blocking the caller: the PATH probe runs the
 * shell asynchronously, so the Electron main process never stalls on it.
 *
 * @param {{
 *   isExecutableFile?: (p: string) => boolean,
 *   candidatePaths?: () => string[],
 * }} [deps]
 * @returns {Promise<string | null>}
 */
function resolveArcaPathAsync(deps = {}) {
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
 * @param {string} serverUrl
 * @param {boolean} [login] Prepare the remote grant instead of starting a host.
 * @returns {string[]}
 */
function buildArcaArgs(serverUrl, login = false) {
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

function buildConnectArgs(serverUrl) {
  return buildArcaArgs(serverUrl);
}

function buildLoginArgs(serverUrl) {
  return buildArcaArgs(serverUrl, true);
}

/**
 * Map a failed connect run to an actionable user-facing result. Matched
 * against known arca / omnigent CLI failure shapes; anything unrecognized
 * falls through to the captured output.
 *
 * `errorKind` lets callers offer the one fix that applies (retry, sign in on
 * Arca, or `arca login` on this machine).
 *
 * @param {{ code: number | null, stdout: string, stderr: string, timedOut?: boolean }} run
 * @returns {{ ok: false, error: string, errorKind: ArcaErrorKind, authError?: boolean }}
 */
function describeConnectFailure(run) {
  const output = `${run.stderr}\n${run.stdout}`;
  if (run.timedOut) {
    return {
      ok: false,
      errorKind: "timeout",
      error:
        "Connecting to Arca timed out. The instance may still be starting — " +
        "check `arca status` and try again.",
    };
  }
  // `omni host --non-interactive` fails loud with a sign-in hint when the
  // Arca box's Databricks credentials can't mint a server token.
  if (/OMNIGENT_AUTH_REQUIRED|not signed in|authentication failed \(HTTP 401\)/i.test(output)) {
    return {
      ok: false,
      authError: true,
      errorKind: "omni-auth",
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
      errorKind: "missing-remote-cli",
      error:
        "`isaac omni` isn't available on the Arca instance. " +
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
    };
  }
  if (/error connecting to arca/i.test(output)) {
    return {
      ok: false,
      errorKind: "unreachable",
      error:
        "Couldn't reach the Arca instance. Try `arca stop && arca start` in a terminal, " +
        "then connect again.",
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
 * Parse the status object even when the CLI surrounds it with notices.
 * @param {string} stdout
 * @returns {{ state: string | null, hasInstance: boolean, shutdownAt: number | null, rawShutdownTime: string | null } | null}
 */
function parseArcaStatus(stdout) {
  const isStatus = (value) => {
    const hasInstance =
      Object.hasOwn(value, "instance") &&
      (value.instance === null ||
        (typeof value.instance === "object" && !Array.isArray(value.instance)));
    return (
      typeof value.status === "string" && (hasInstance || Object.hasOwn(value, "shutdown_time"))
    );
  };
  const parseObject = (text) => {
    try {
      const value = JSON.parse(text);
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
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
      : status.instance.shutdown_time;
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

/**
 * Run a headless Arca command and capture both output streams.
 * @param {string} arcaPath
 * @param {string[]} args
 * @param {{ spawn?: typeof spawn, timeoutMs: number, killGraceMs?: number, maxOutputBytes?: number }} options
 * @returns {Promise<{ code: number | null, stdout: string, stderr: string, timedOut?: boolean, spawnError?: Error }>}
 */
function runArca(arcaPath, args, options) {
  return new Promise((resolve) => {
    let child;
    try {
      child = (options.spawn || spawn)(arcaPath, args, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
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
    let exitCode = null;
    let exitGraceTimer;
    let killTimer;
    let timedOut = false;
    const killTree = (signal) => {
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
    const onStdout = (chunk) => {
      if (stdoutBytes >= (options.maxOutputBytes ?? Infinity)) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
      const retained = bytes.subarray(0, (options.maxOutputBytes ?? Infinity) - stdoutBytes);
      stdout += stdoutDecoder.write(retained);
      stdoutBytes += retained.length;
    };
    const onStderr = (chunk) => {
      if (stderrBytes >= (options.maxOutputBytes ?? Infinity)) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
      const retained = bytes.subarray(0, (options.maxOutputBytes ?? Infinity) - stderrBytes);
      stderr += stderrDecoder.write(retained);
      stderrBytes += retained.length;
    };
    const settle = (result, fromClose = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(exitGraceTimer);
      clearTimeout(killTimer);
      stdout += stdoutDecoder.end();
      stderr += stderrDecoder.end();
      if (!fromClose) {
        for (const [stream, listener] of [
          [child.stdout, onStdout],
          [child.stderr, onStderr],
        ]) {
          try {
            stream?.off("data", listener);
            stream?.destroy?.();
          } catch {
            // A child may already have closed the pipe.
          }
        }
      }
      resolve({ code: result.code, stdout, stderr, timedOut, ...result });
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
      exitCode = code;
      exitGraceTimer = setTimeout(() => settle({ code }), 500);
    });
    child.on("close", (code) => settle({ code: exited ? exitCode : code }, true));
  });
}

/**
 * Turn a failed status or extend run into an actionable result.
 * @param {{ code: number | null, stdout: string, stderr: string, timedOut?: boolean }} run
 * @param {"status" | "extend"} action
 * @returns {{ ok: false, errorKind: ArcaErrorKind, error: string }}
 */
function describeArcaCliFailure(run, action) {
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

/**
 * Read this machine's Arca instance state and shutdown time.
 * @param {{ resolveArcaPath?: () => string | null | Promise<string | null>, spawn?: typeof spawn, timeoutMs?: number, killGraceMs?: number }} [deps]
 * @returns {Promise<{ ok: true, state: string | null, hasInstance: boolean, shutdownAt: number | null, rawShutdownTime: string | null } | { ok: false, errorKind: ArcaErrorKind, error: string }>}
 */
async function readArcaStatus(deps = {}) {
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
        error: `Couldn't run arca: ${run.spawnError.message}`,
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
    return { ok: false, errorKind: "unknown", error: `Couldn't run arca: ${error.message}` };
  }
}

/**
 * Extend this machine's Arca instance using an approved mode.
 * @param {string} mode
 * @param {{ resolveArcaPath?: () => string | null | Promise<string | null>, spawn?: typeof spawn, timeoutMs?: number, killGraceMs?: number }} [deps]
 * @returns {Promise<{ ok: true, shutdownAt: number | null } | { ok: false, errorKind: ArcaErrorKind, error: string }>}
 */
async function runArcaExtend(mode, deps = {}) {
  if (!ARCA_EXTEND_MODES.includes(mode))
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
        error: `Couldn't run arca: ${run.spawnError.message}`,
      };
    if (run.code !== 0 || run.timedOut) return describeArcaCliFailure(run, "extend");
    const updated = await readArcaStatus({ ...deps, resolveArcaPath: () => arcaPath });
    if (!updated.ok) console.warn("[arca] status verification after extend failed:", updated.error);
    return { ok: true, shutdownAt: updated.ok ? updated.shutdownAt : null };
  } catch (error) {
    return { ok: false, errorKind: "unknown", error: `Couldn't run arca: ${error.message}` };
  }
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
 *   onIdentityUnavailable?: (reason: string) => void,
 * }} [deps]
 * @returns {{
 *   command: string | null,
 *   promise: Promise<{
 *     ok: boolean,
 *     alreadyRunning?: boolean,
 *     identity?: { serverUrl: string, hostId: string },
 *     error?: string,
 *     authError?: boolean,
 *     canceled?: boolean,
 *   }>,
 *   cancel: () => void,
 * }}
 */
function startArcaCommand(serverUrl, deps = {}, login = false) {
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
    args = buildArcaArgs(serverUrl, login);
  } catch (error) {
    return {
      command: null,
      promise: Promise.resolve({ ok: false, error: `Invalid server URL: ${error.message}` }),
      cancel: () => {},
    };
  }
  const spawnFn = deps.spawn || spawn;
  let child;
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
  let settle;
  const promise = new Promise((resolve) => {
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
      settle({ ok: false, error: `Couldn't run arca: ${error.message}` });
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
      settle(describeConnectFailure({ code, stdout, stderr }));
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

function startArcaConnect(serverUrl, deps = {}) {
  return startArcaCommand(serverUrl, deps);
}

function startArcaLogin(serverUrl, deps = {}) {
  return startArcaCommand(serverUrl, deps, true);
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
  ARCA_EXTEND_MODES,
  STATUS_TIMEOUT_MS,
  EXTEND_TIMEOUT_MS,
  parseArcaStatus,
  readArcaStatus,
  runArcaExtend,
  CONNECT_TIMEOUT_MS,
  buildConnectArgs,
  buildLoginArgs,
  connectArcaHost,
  describeConnectFailure,
  isExecutableFile,
  resolveArcaPath,
  resolveArcaPathAsync,
  startArcaConnect,
  startArcaLogin,
};
