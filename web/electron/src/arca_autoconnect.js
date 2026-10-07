"use strict";

/**
 * Arca auto-connect (Databricks-internal): when a window loads an eligible
 * server, make sure the user's Arca instance is connected to it as a host by
 * running the same idempotent `arca ssh … isaac omni host --background` as the
 * manual "Run on Arca" item — without the consent console, at most once per
 * server target (including the workspace selector) per app launch.
 *
 * The command exits once the remote daemon is up (or reports it was already
 * running), and the daemon then keeps its own outbound tunnel, so nothing here
 * outlives the run. Only the main process starts runs; a retry is allowed
 * only after a failure. Whether the feature is on at all is part of
 * `isEligible`.
 *
 * Electron-free: every dependency is injected so the state machine is
 * unit-testable.
 */

const { normalizeServerUrl } = require("./omnigent_cli");

/** Keep the tail of the command output for failure diagnostics. */
const OUTPUT_TAIL_CHARS = 8000;

/**
 * @typedef {{
 *   state: "unavailable" | "idle" | "starting" | "online" | "failed",
 *   command: string | null,
 *   alreadyRunning?: boolean,
 *   errorKind?: import("./arca").ArcaErrorKind,
 *   error?: string,
 *   startedAt?: number,
 *   finishedAt?: number,
 *   output?: string,
 * }} ArcaStatus
 */

/**
 * @param {{
 *   isEligible: (serverUrl: string) => boolean,
 *   startConnect: (serverUrl: string, onOutput: (text: string) => void) =>
 *     ReturnType<typeof import("./arca").startArcaConnect>,
 *   commandLine: (serverUrl: string) => string | null,
 *   onStatus?: (target: string, status: ArcaStatus) => void,
 *   now?: () => number,
 *   log?: (message: string) => void,
 * }} deps
 */
function createArcaAutoConnect({
  isEligible,
  startConnect,
  commandLine,
  onStatus = () => {},
  now = () => Date.now(),
  log = () => {},
}) {
  /** @type {Map<string, { status: ArcaStatus, run: Promise<ArcaStatus> | null }>} */
  const byTarget = new Map();

  function targetOf(serverUrl) {
    try {
      return normalizeServerUrl(new URL(serverUrl).href);
    } catch {
      return null;
    }
  }

  function baseStatus(serverUrl) {
    if (!isEligible(serverUrl)) return { state: "unavailable", command: null };
    return { state: "idle", command: commandLine(serverUrl) };
  }

  function publish(target, status) {
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
   *
   * @param {string | null | undefined} serverUrl
   * @returns {ArcaStatus}
   */
  function getStatus(serverUrl) {
    const target = serverUrl ? targetOf(serverUrl) : null;
    if (!target) return { state: "unavailable", command: null };
    const base = baseStatus(serverUrl);
    if (base.state === "unavailable") return base;
    const entry = byTarget.get(target);
    if (!entry) return base;
    return entry.status;
  }

  function runConnect(serverUrl, target, onOutput) {
    const command = commandLine(serverUrl);
    let output = "";
    const status = {
      state: "starting",
      command,
      startedAt: now(),
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
      const base = { command, startedAt: status.startedAt };
      const finishedAt = now();
      const next = result.ok
        ? { ...base, state: "online", alreadyRunning: result.alreadyRunning === true, finishedAt }
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
    byTarget.get(target).run = run;
    return run;
  }

  /**
   * Launch-time entry point: connect Arca for `serverUrl` unless it isn't
   * eligible or already ran for this target this launch. A
   * second window or a reload shares the in-flight run.
   *
   * @param {string | null | undefined} serverUrl
   * @param {(text: string) => void} [onOutput] Streams a new run's output;
   *   joining a run already in flight gets only its outcome.
   * @returns {Promise<ArcaStatus>}
   */
  function ensure(serverUrl, onOutput) {
    const current = getStatus(serverUrl);
    if (current.state !== "idle") {
      const entry = byTarget.get(targetOf(serverUrl));
      return entry?.run ?? Promise.resolve(current);
    }
    return runConnect(serverUrl, targetOf(serverUrl), onOutput);
  }

  /**
   * User-requested retry. Only a failed run may be retried, so a server page
   * can't use this to re-run the command at will.
   *
   * @param {string | null | undefined} serverUrl
   * @param {(text: string) => void} [onOutput] As for ensure.
   * @returns {Promise<ArcaStatus>}
   */
  function retry(serverUrl, onOutput) {
    const running = inFlight(serverUrl);
    if (running) return running;
    const current = getStatus(serverUrl);
    if (current.state !== "failed") return Promise.resolve(current);
    return runConnect(serverUrl, targetOf(serverUrl), onOutput);
  }

  /**
   * The in-flight run for `serverUrl`, if any — lets the manual connect share
   * it instead of racing a second `arca ssh`.
   *
   * @param {string | null | undefined} serverUrl
   * @returns {Promise<ArcaStatus> | null}
   */
  function inFlight(serverUrl) {
    const target = serverUrl ? targetOf(serverUrl) : null;
    return (target && byTarget.get(target)?.run) || null;
  }

  return { ensure, retry, getStatus, inFlight };
}

module.exports = { OUTPUT_TAIL_CHARS, createArcaAutoConnect };
