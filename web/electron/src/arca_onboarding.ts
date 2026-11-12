import { spawn } from "node:child_process";

import * as arcaModule from "./arca.ts";
import type {
  ArcaChild,
  ArcaChildOutput,
  ArcaCommandFailure,
  ArcaConnectFailure,
  SpawnArca,
} from "./arca.ts";
import { withTestOverrides } from "./testOverrides.ts";

const { buildConnectArgs, describeConnectFailure, resolveArcaPath } = withTestOverrides(
  "arca",
  arcaModule,
);

// Allows time for the first start and both browser sign-ins.
export const SETUP_TIMEOUT_MS = 15 * 60 * 1000;

/** Prepare both remote sign-ins during initial onboarding. */
export function buildSetupArgs(serverUrl: string): string[] {
  const args = buildConnectArgs(serverUrl);
  // A piped SSH command has no TTY even when --non-interactive is omitted.
  args.splice(1, 0, "-tt");
  args.splice(args.indexOf("--non-interactive"), 1);
  // Remote non-login shells don't set BROWSER, so set it for the sign-in.
  args.splice(args.indexOf("isaac"), 0, "env", 'BROWSER="$HOME/.arca-client/tools/www-browser"');
  return args;
}

/** Keep OAuth tickets private while retaining known, constant diagnostics. */
function describeSetupFailure(
  code: number | null,
  stdout: string,
  stderr: string,
): ArcaConnectFailure {
  const failure = describeConnectFailure({ code, stdout, stderr });
  if (failure.errorKind !== "unknown" && failure.errorKind !== "omni-auth") return failure;
  if (code === 255) {
    return {
      ok: false,
      errorKind: "unreachable",
      error:
        "Arca sign-in couldn't reach the remote command. Check `arca ssh` in a terminal and try again.",
      errorMessage: "Arca sign-in couldn't reach the remote command.",
      errorAction: "Check `arca ssh` in a terminal and try again.",
    };
  }
  return {
    ok: false,
    errorKind: failure.errorKind,
    ...(failure.authError ? { authError: true } : {}),
    error:
      "Arca setup didn't complete. Check Arca Companion, finish browser sign-in, and try again.",
    errorMessage: "Arca setup didn't complete.",
    errorAction: "Check Arca Companion, finish browser sign-in, and try again.",
  };
}

/** What the setup command uses; each defaults to the real one. */
export interface StartArcaSetupDeps {
  resolveArcaPath?: (() => string | null) | undefined;
  spawn?: SpawnArca | undefined;
  timeoutMs?: number | undefined;
  killGraceMs?: number | undefined;
}

export type ArcaSetupResult = { ok: true; alreadyRunning: boolean } | ArcaCommandFailure;

/** A started setup; `command` is null when nothing was started. */
export interface ArcaSetup {
  command: string | null;
  promise: Promise<ArcaSetupResult>;
  cancel: () => void;
}

/** A child's exit code as Node reports it on `exit` and `close`. */
function exitCodeOf(value: unknown): number | null {
  // Node passes a number, or null after a signal; the cast keeps the value as is.
  return value as number | null;
}

/**
 * Onboarding owns this interactive process; existing host connections keep
 * their noninteractive command runner and authentication fallback.
 */
export function startArcaSetup(serverUrl: string, deps: StartArcaSetupDeps = {}): ArcaSetup {
  const arcaPath = (deps.resolveArcaPath || resolveArcaPath)();
  if (!arcaPath) {
    return {
      command: null,
      promise: Promise.resolve({ ok: false, error: "The arca CLI was not found on this machine." }),
      cancel: () => {},
    };
  }
  let args: string[];
  try {
    args = buildSetupArgs(serverUrl);
  } catch (error) {
    return {
      command: null,
      // Read like `error.message`: a thrown null or undefined throws here.
      promise: Promise.resolve({
        ok: false,
        error: `Invalid server URL: ${(error as { message?: unknown }).message}`,
      }),
      cancel: () => {},
    };
  }

  let cancel = () => {};
  const promise = new Promise<ArcaSetupResult>((resolve) => {
    let child: ArcaChild;
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
    let exitCode: number | null = null;
    let exitGraceTimer: ReturnType<typeof setTimeout> | undefined;
    let killTimer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    let canceled = false;
    const canceledResult: ArcaSetupResult = {
      ok: false,
      canceled: true,
      error: "Arca sign-in was canceled.",
    };
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
      if (!canceled) stdout = (stdout + String(chunk)).slice(-8000);
    };
    const onStderr = (chunk: unknown) => {
      if (!canceled) stderr = (stderr + String(chunk)).slice(-8000);
    };
    const settle = (result: ArcaSetupResult, fromClose = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(exitGraceTimer);
      // Closed pipes do not mean descendants with separate stdio have exited.
      if (!canceled && !timedOut) clearTimeout(killTimer);
      else killTimer?.unref?.();
      if (!fromClose) {
        const streams: [ArcaChildOutput | null | undefined, (chunk: unknown) => void][] = [
          [child.stdout, onStdout],
          [child.stderr, onStderr],
        ];
        for (const [stream, listener] of streams) {
          stream?.off("data", listener);
          stream?.destroy?.();
        }
      }
      resolve(result);
    };
    const finish = (code: number | null, fromClose = false) => {
      const result: ArcaSetupResult = canceled
        ? canceledResult
        : timedOut
          ? {
              ok: false,
              errorKind: "timeout",
              error:
                "Arca sign-in timed out. Check Arca Companion, finish browser sign-in, and try again.",
              errorMessage: "Arca sign-in timed out.",
              errorAction: "Check Arca Companion, finish browser sign-in, and try again.",
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
      // stop() just scheduled it.
      killTimer!.unref?.();
      resolve(canceledResult);
    };
    child.stdout?.on("data", onStdout);
    child.stderr?.on("data", onStderr);
    child.on("error", () => settle({ ok: false, error: "Couldn't start the Arca setup command." }));
    child.on("exit", (code) => {
      if (settled) return;
      exited = true;
      exitCode = exitCodeOf(code);
      if (!canceled && !timedOut) exitGraceTimer = setTimeout(() => finish(exitCodeOf(code)), 500);
    });
    child.on("close", (code) => finish(exited ? exitCode : exitCodeOf(code), true));
  });
  return { command: `arca ${args.join(" ")}`, promise, cancel: () => cancel() };
}
