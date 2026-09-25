"use strict";

/**
 * Arca auto-connect (Databricks-internal): when a window loads an eligible
 * server, make sure the user's Arca instance is connected to it as a host by
 * running the same idempotent `arca ssh … isaac omni host --background` as the
 * manual "Run on Arca" item — without the consent console, at most once per
 * server origin per app launch.
 *
 * The command exits once the remote daemon is up (or reports it was already
 * running), and the daemon then keeps its own outbound tunnel, so nothing here
 * outlives the run. The renderer only reads status and may ask for a retry
 * after a failure; it can never start a run on its own. Whether the feature
 * is on at all is part of `isEligible`.
 *
 * Electron-free: every dependency is injected so the state machine is
 * unit-testable.
 */

/** Keep the tail of the command output for the status "Details" view. */
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
 *   onStatus: (origin: string, status: ArcaStatus) => void,
 *   now?: () => number,
 *   log?: (message: string) => void,
 * }} deps
 */
function createArcaAutoConnect({
  isEligible,
  startConnect,
  commandLine,
  onStatus,
  now = () => Date.now(),
  log = () => {},
}) {
  /** @type {Map<string, { status: ArcaStatus, run: Promise<ArcaStatus> | null }>} */
  const byOrigin = new Map();

  function originOf(serverUrl) {
    try {
      return new URL(serverUrl).origin;
    } catch {
      return null;
    }
  }

  function baseStatus(serverUrl) {
    if (!isEligible(serverUrl)) return { state: "unavailable", command: null };
    return { state: "idle", command: commandLine(serverUrl) };
  }

  function publish(origin, status) {
    const entry = byOrigin.get(origin) ?? { status, run: null };
    entry.status = status;
    byOrigin.set(origin, entry);
    onStatus(origin, status);
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
    const origin = serverUrl ? originOf(serverUrl) : null;
    if (!origin) return { state: "unavailable", command: null };
    const base = baseStatus(serverUrl);
    if (base.state === "unavailable") return base;
    const entry = byOrigin.get(origin);
    if (!entry) return base;
    return entry.status;
  }

  function runConnect(serverUrl, origin) {
    const command = commandLine(serverUrl);
    let output = "";
    const status = {
      state: "starting",
      command,
      startedAt: now(),
    };
    publish(origin, status);
    log(`arca auto-connect: running against ${origin}`);
    const connect = startConnect(serverUrl, (text) => {
      output = (output + text).slice(-OUTPUT_TAIL_CHARS);
      const entry = byOrigin.get(origin);
      if (entry?.status.state === "starting") entry.status = { ...entry.status, output };
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
      const entry = byOrigin.get(origin);
      if (entry) entry.run = null;
      return publish(origin, next);
    });
    byOrigin.get(origin).run = run;
    return run;
  }

  /**
   * Launch-time entry point: connect Arca for `serverUrl` unless it isn't
   * eligible or already ran for this origin this launch. A
   * second window or a reload shares the in-flight run.
   *
   * @param {string | null | undefined} serverUrl
   * @returns {Promise<ArcaStatus>}
   */
  function ensure(serverUrl) {
    const current = getStatus(serverUrl);
    if (current.state !== "idle") {
      const entry = byOrigin.get(originOf(serverUrl));
      return entry?.run ?? Promise.resolve(current);
    }
    return runConnect(serverUrl, originOf(serverUrl));
  }

  /**
   * User-requested retry. Only a failed run may be retried, so a server page
   * can't use this to re-run the command at will.
   *
   * @param {string | null | undefined} serverUrl
   * @returns {Promise<ArcaStatus>}
   */
  function retry(serverUrl) {
    const running = inFlight(serverUrl);
    if (running) return running;
    const current = getStatus(serverUrl);
    if (current.state !== "failed") return Promise.resolve(current);
    return runConnect(serverUrl, originOf(serverUrl));
  }

  /**
   * The in-flight run for `serverUrl`, if any — lets the manual connect share
   * it instead of racing a second `arca ssh`.
   *
   * @param {string | null | undefined} serverUrl
   * @returns {Promise<ArcaStatus> | null}
   */
  function inFlight(serverUrl) {
    const origin = serverUrl ? originOf(serverUrl) : null;
    return (origin && byOrigin.get(origin)?.run) || null;
  }

  return { ensure, retry, getStatus, inFlight };
}

module.exports = { OUTPUT_TAIL_CHARS, createArcaAutoConnect };
