"use strict";

const { spawn } = require("node:child_process");
const { buildConnectArgs, describeConnectFailure, resolveArcaPath } = require("./arca");

// Cold start plus the Isaac workspace and Omnigent browser sign-ins.
const SETUP_TIMEOUT_MS = 15 * 60 * 1000;

/** Prepare both remote sign-ins during initial onboarding. */
function buildSetupArgs(serverUrl) {
  const args = buildConnectArgs(serverUrl);
  // A piped SSH command has no TTY even when --non-interactive is omitted.
  args.splice(1, 0, "-tt");
  args.splice(args.indexOf("--non-interactive"), 1);
  // Remote command shells need not load Companion's BROWSER environment.
  args.splice(args.indexOf("isaac"), 0, "env", 'BROWSER="$HOME/.arca-client/tools/www-browser"');
  return args;
}

/** Keep OAuth tickets private while retaining known, constant diagnostics. */
function describeSetupFailure(code, stdout, stderr) {
  const failure = describeConnectFailure({ code, stdout, stderr });
  if (failure.errorKind !== "unknown" && failure.errorKind !== "omni-auth") return failure;
  if (code === 255) {
    return {
      ok: false,
      errorKind: "unreachable",
      error:
        "Arca sign-in couldn't reach the remote command. Check `arca ssh` in a terminal and try again.",
    };
  }
  return {
    ok: false,
    errorKind: failure.errorKind,
    ...(failure.authError ? { authError: true } : {}),
    error:
      "Arca setup didn't complete. Check Arca Companion, finish browser sign-in, and try again.",
  };
}

/**
 * Onboarding owns this interactive process; existing host connections keep
 * their noninteractive command runner and authentication fallback.
 * @param {string} serverUrl
 * @param {{ resolveArcaPath?: () => string | null, spawn?: typeof spawn, timeoutMs?: number, killGraceMs?: number }} [deps]
 */
function startArcaSetup(serverUrl, deps = {}) {
  const arcaPath = (deps.resolveArcaPath || resolveArcaPath)();
  if (!arcaPath) {
    return {
      command: null,
      promise: Promise.resolve({ ok: false, error: "The arca CLI was not found on this machine." }),
      cancel: () => {},
    };
  }
  let args;
  try {
    args = buildSetupArgs(serverUrl);
  } catch (error) {
    return {
      command: null,
      promise: Promise.resolve({ ok: false, error: `Invalid server URL: ${error.message}` }),
      cancel: () => {},
    };
  }

  let cancel = () => {};
  const promise = new Promise((resolve) => {
    let child;
    try {
      child = (deps.spawn || spawn)(arcaPath, args, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
      });
    } catch {
      resolve({ ok: false, error: "Couldn't start the Arca setup command." });
      return;
    }
    let stdout = "";
    let stderr = "";
    let settled = false;
    let exited = false;
    let exitCode = null;
    let exitGraceTimer;
    let killTimer;
    let timedOut = false;
    let canceled = false;
    const canceledResult = { ok: false, canceled: true, error: "Arca sign-in was canceled." };
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
      if (!canceled) stdout = (stdout + String(chunk)).slice(-8000);
    };
    const onStderr = (chunk) => {
      if (!canceled) stderr = (stderr + String(chunk)).slice(-8000);
    };
    const settle = (result, fromClose = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(exitGraceTimer);
      // Closed pipes do not mean descendants with separate stdio have exited.
      if (!canceled && !timedOut) clearTimeout(killTimer);
      else killTimer?.unref?.();
      if (!fromClose) {
        for (const [stream, listener] of [
          [child.stdout, onStdout],
          [child.stderr, onStderr],
        ]) {
          stream?.off("data", listener);
          stream?.destroy?.();
        }
      }
      resolve(result);
    };
    const finish = (code, fromClose = false) => {
      const result = canceled
        ? canceledResult
        : timedOut
          ? {
              ok: false,
              errorKind: "timeout",
              error:
                "Arca sign-in timed out. Check Arca Companion, finish browser sign-in, and try again.",
            }
          : code === 0
            ? { ok: true, alreadyRunning: /already running/i.test(stdout + stderr) }
            : describeSetupFailure(code, stdout, stderr);
      settle(result, fromClose);
    };
    const stop = () => {
      clearTimeout(timer);
      clearTimeout(exitGraceTimer);
      clearTimeout(killTimer);
      killTree("SIGTERM");
      killTimer = setTimeout(() => {
        killTree("SIGKILL");
        finish(null);
      }, deps.killGraceMs ?? 5000);
    };
    const timer = setTimeout(() => {
      if (exited) {
        finish(exitCode);
        return;
      }
      timedOut = true;
      stop();
    }, deps.timeoutMs ?? SETUP_TIMEOUT_MS);
    cancel = () => {
      if (settled || canceled) return;
      canceled = true;
      stop();
      killTimer.unref?.();
      resolve(canceledResult);
    };
    child.stdout?.on("data", onStdout);
    child.stderr?.on("data", onStderr);
    child.on("error", () => settle({ ok: false, error: "Couldn't start the Arca setup command." }));
    child.on("exit", (code) => {
      if (settled) return;
      exited = true;
      exitCode = code;
      if (!canceled && !timedOut) exitGraceTimer = setTimeout(() => finish(code), 500);
    });
    child.on("close", (code) => finish(exited ? exitCode : code, true));
  });
  return { command: `arca ${args.join(" ")}`, promise, cancel: () => cancel() };
}

module.exports = { buildSetupArgs, startArcaSetup, SETUP_TIMEOUT_MS };
