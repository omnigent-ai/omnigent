/**
 * Arca auto-connect: when a window loads an eligible server, connect the user's
 * Arca instance to it as a host by running the same idempotent
 * `arca ssh … isaac omni host --background` command as the manual "Run on Arca"
 * item, without the consent console. Runs at most once per server target
 * (including the workspace selector) per app launch. Only the main process
 * starts runs; a retry is allowed only after a failure. Whether the feature is
 * on at all is part of `isEligible`.
 *
 * Electron-free: every dependency is injected so the state machine is
 * unit-testable.
 */

import type { ArcaErrorKind } from "./arca.ts";
import type { ArcaIdentity } from "./arcaIdentity.ts";
import { normalizeServerUrl } from "./omnigent_cli.ts";

/** Keep the tail of the command output for failure diagnostics. */
export const OUTPUT_TAIL_CHARS = 8000;

export interface ArcaAutoConnectStatus {
  state: "unavailable" | "idle" | "starting" | "online" | "failed";
  command: string | null;
  alreadyRunning?: boolean;
  identity?: ArcaIdentity | undefined;
  errorKind?: ArcaErrorKind;
  error?: string;
  startedAt?: number;
  finishedAt?: number;
  output?: string;
}

/** How a connect ended, as far as auto-connect reads it. */
export interface ArcaAutoConnectResult {
  ok: boolean;
  alreadyRunning?: boolean | undefined;
  identity?: ArcaIdentity | undefined;
  errorKind?: ArcaErrorKind | undefined;
  error?: string | undefined;
}

/** A connect in flight. */
export interface ArcaAutoConnectRun {
  promise: Promise<ArcaAutoConnectResult>;
}

export interface ArcaAutoConnectDeps {
  isEligible: (serverUrl: string) => boolean;
  startConnect: (serverUrl: string, onOutput: (text: string) => void) => ArcaAutoConnectRun;
  commandLine: (serverUrl: string) => string | null;
  onStatus?: ((target: string, status: ArcaAutoConnectStatus) => void) | undefined;
  now?: (() => number) | undefined;
  log?: ((message: string) => void) | undefined;
}

export interface ArcaAutoConnect {
  ensure: (
    serverUrl: string | null | undefined,
    onOutput?: (text: string) => void,
  ) => Promise<ArcaAutoConnectStatus>;
  retry: (
    serverUrl: string | null | undefined,
    onOutput?: (text: string) => void,
  ) => Promise<ArcaAutoConnectStatus>;
  getStatus: (serverUrl: string | null | undefined) => ArcaAutoConnectStatus;
  inFlight: (serverUrl: string | null | undefined) => Promise<ArcaAutoConnectStatus> | null;
}

interface TargetEntry {
  status: ArcaAutoConnectStatus;
  run: Promise<ArcaAutoConnectStatus> | null;
}

export function createArcaAutoConnect({
  isEligible,
  startConnect,
  commandLine,
  onStatus = () => {},
  now = () => Date.now(),
  log = () => {},
}: ArcaAutoConnectDeps): ArcaAutoConnect {
  const byTarget = new Map<string, TargetEntry>();

  function targetOf(serverUrl: unknown): string | null {
    try {
      // The constructor stringifies any value itself; the cast only lets TypeScript pass it.
      return normalizeServerUrl(new URL(serverUrl as string).href);
    } catch {
      return null;
    }
  }

  function baseStatus(serverUrl: string): ArcaAutoConnectStatus {
    if (!isEligible(serverUrl)) return { state: "unavailable", command: null };
    return { state: "idle", command: commandLine(serverUrl) };
  }

  function publish(target: string, status: ArcaAutoConnectStatus): ArcaAutoConnectStatus {
    const entry = byTarget.get(target) ?? { status, run: null };
    entry.status = status;
    byTarget.set(target, entry);
    onStatus(target, status);
    return status;
  }

  /**
   * The current status for `serverUrl`. A server that was never run reports
   * its eligibility; eligibility is re-checked so turning the feature off or
   * removing arca hides the status without a restart.
   */
  function getStatus(serverUrl: string | null | undefined): ArcaAutoConnectStatus {
    const target = serverUrl ? targetOf(serverUrl) : null;
    if (!target) return { state: "unavailable", command: null };
    // A target exists only for a truthy URL.
    const base = baseStatus(serverUrl!);
    if (base.state === "unavailable") return base;
    const entry = byTarget.get(target);
    if (!entry) return base;
    return entry.status;
  }

  function runConnect(
    serverUrl: string,
    target: string,
    onOutput: ((text: string) => void) | undefined,
  ): Promise<ArcaAutoConnectStatus> {
    const command = commandLine(serverUrl);
    let output = "";
    const startedAt = now();
    const status: ArcaAutoConnectStatus = {
      state: "starting",
      command,
      startedAt,
    };
    publish(target, status);
    log(`arca auto-connect: running against ${target}`);
    const connect = startConnect(serverUrl, (text) => {
      output = (output + text).slice(-OUTPUT_TAIL_CHARS);
      const entry = byTarget.get(target);
      if (entry?.status.state === "starting") entry.status = { ...entry.status, output };
      // After the shared bookkeeping, so a throwing caller can't skip it.
      onOutput?.(text);
    });
    const run = connect.promise.then((result) => {
      const base = { command, startedAt };
      const finishedAt = now();
      const next: ArcaAutoConnectStatus = result.ok
        ? {
            ...base,
            state: "online",
            alreadyRunning: result.alreadyRunning === true,
            identity: result.identity,
            finishedAt,
          }
        : {
            ...base,
            state: "failed",
            errorKind: result.errorKind ?? "unknown",
            error: result.error ?? "Connecting to Arca failed.",
            finishedAt,
            output,
          };
      log(`arca auto-connect: ${next.state}${next.errorKind ? ` (${next.errorKind})` : ""}`);
      const entry = byTarget.get(target);
      if (entry) entry.run = null;
      return publish(target, next);
    });
    // publish() above created the entry; it is never removed.
    byTarget.get(target)!.run = run;
    return run;
  }

  /**
   * Launch-time entry point: connect Arca for `serverUrl` unless it isn't
   * eligible or already ran for this target this launch. A
   * second window or a reload shares the in-flight run.
   *
   * @param onOutput Streams a new run's output; joining a run already in
   *   flight gets only its outcome.
   */
  function ensure(
    serverUrl: string | null | undefined,
    onOutput?: (text: string) => void,
  ): Promise<ArcaAutoConnectStatus> {
    const current = getStatus(serverUrl);
    if (current.state !== "idle") {
      const target = targetOf(serverUrl);
      const entry = target === null ? undefined : byTarget.get(target);
      return entry?.run ?? Promise.resolve(current);
    }
    // "idle" means getStatus found a target for this URL.
    return runConnect(serverUrl!, targetOf(serverUrl!)!, onOutput);
  }

  /**
   * User-requested retry. Only a failed run may be retried, so a server page
   * can't use this to re-run the command at will.
   *
   * @param onOutput As for ensure.
   */
  function retry(
    serverUrl: string | null | undefined,
    onOutput?: (text: string) => void,
  ): Promise<ArcaAutoConnectStatus> {
    const running = inFlight(serverUrl);
    if (running) return running;
    const current = getStatus(serverUrl);
    if (current.state !== "failed") return Promise.resolve(current);
    // "failed" means getStatus found a target for this URL.
    return runConnect(serverUrl!, targetOf(serverUrl!)!, onOutput);
  }

  /**
   * The in-flight run for `serverUrl`, if any — lets the manual connect share
   * it instead of racing a second `arca ssh`.
   */
  function inFlight(serverUrl: string | null | undefined): Promise<ArcaAutoConnectStatus> | null {
    const target = serverUrl ? targetOf(serverUrl) : null;
    return (target && byTarget.get(target)?.run) || null;
  }

  return { ensure, retry, getStatus, inFlight };
}
