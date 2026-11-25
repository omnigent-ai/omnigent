// The state machine for resuming a session's stopped managed sandbox without a
// message. It holds no React and no timers: the store feeds it the request's
// outcome, the host's liveness, the launch stage, and the clock.

import { isSandboxLaunchStopped } from "@/lib/sandboxStatus";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type { SandboxStatus } from "@/lib/types";

/** How long a resume may go without the host online or a launch stage reporting progress. */
export const SANDBOX_RESUME_QUIET_LIMIT_MS = 2 * 60_000;

/** How long a resume may run in all, even while a launch stage keeps reporting progress. */
export const SANDBOX_RESUME_TOTAL_LIMIT_MS = 5 * 60_000;

export const SANDBOX_RESUME_TIMED_OUT = "The sandbox didn't come online. Try resuming it again.";

interface ActiveResume {
  /** Distinguishes this attempt's late answers and timers from a retry's. */
  attempt: number;
  startedAt: number;
  /** The last sign of progress: the server's answer or a new launch stage. */
  progressAt: number;
  /** The launch stage at the click, so a failure left from before can't end this attempt. */
  launchAtStart: string;
  /** The latest launch stage seen; a new one counts as progress. */
  lastLaunch: string;
  stageChanged: boolean;
  launchInProgress: boolean;
}

export type SandboxResumeState =
  | ({ phase: "requesting" } & ActiveResume)
  | ({ phase: "awaiting_online" } & ActiveResume)
  | { phase: "failed"; attempt: number; message: string };

export type SandboxResumeEvent =
  /** `attempt` is unique for the app's lifetime, since an attempt's state can be cleared while its request is out. */
  | { type: "start"; attempt: number; now: number; launch: SandboxStatus | null }
  | { type: "request_succeeded"; attempt: number; now: number }
  | { type: "request_failed"; attempt: number; message: string }
  | { type: "host_online" }
  | { type: "launch"; launch: SandboxStatus | null; now: number }
  | { type: "tick"; now: number };

/** Identifies a reported launch stage by value; snapshots rebuild the object. */
export function sandboxLaunchKey(launch: SandboxStatus | null): string {
  return launch ? JSON.stringify([launch.stage, launch.error, launch.details]) : "";
}

/** Where a session's sandbox is, as Resume surfaces present it. */
export type SandboxResumeStatus =
  /** Nothing to resume: the sandbox is up, or the session has none. */
  "none" | "asleep" | "resuming" | "failed";

/**
 * What Resume surfaces show, from the session's resume and whether its sandbox
 * reads asleep. A resume that started the agent can read as starting, not
 * asleep, before its host is online, so a running resume wins.
 */
export function sandboxResumeStatus(
  resume: SandboxResumeState | undefined,
  asleep: boolean,
): SandboxResumeStatus {
  if (isSandboxResumeActive(resume)) return "resuming";
  if (!asleep) return "none";
  return resume?.phase === "failed" ? "failed" : "asleep";
}

/** What a session's Resume surfaces show; see {@link sandboxResumeDisplay}. */
export interface SandboxResumeDisplay {
  /** What panels show, counting a launch the server reports. */
  status: SandboxResumeStatus;
  /** The session's own resume alone, which menus offer Resume by. */
  resumeStatus: SandboxResumeStatus;
  failure: string | null;
}

/**
 * The session's resume as its surfaces show it. A launch the server reports (a wake from
 * another tab, before a reload, or a sent message) reads as resuming while the sandbox is
 * asleep or starting; the session's own failure wins, so a stalled stage keeps its Retry.
 */
export function sandboxResumeDisplay(
  resume: SandboxResumeState | undefined,
  liveness: SessionLiveness["kind"],
  launchInProgress: boolean,
): SandboxResumeDisplay {
  // A host still starting after a failed resume isn't up yet, so the failure keeps its Retry.
  const resumeStatus =
    resume?.phase === "failed" && liveness === "starting"
      ? "failed"
      : sandboxResumeStatus(resume, liveness === "host_asleep");
  const waitingOnLaunch =
    launchInProgress && (liveness === "host_asleep" || liveness === "starting");
  const status =
    waitingOnLaunch && (resumeStatus === "asleep" || resumeStatus === "none")
      ? "resuming"
      : resumeStatus;
  return { status, resumeStatus, failure: sandboxResumeFailure(resume, status) };
}

/** Why the last resume failed, while it shows as failed. */
export function sandboxResumeFailure(
  resume: SandboxResumeState | undefined,
  status: SandboxResumeStatus,
): string | null {
  return status === "failed" && resume?.phase === "failed" ? resume.message : null;
}

export function isSandboxResumeActive(
  state: SandboxResumeState | undefined,
): state is Extract<SandboxResumeState, ActiveResume> {
  return state?.phase === "requesting" || state?.phase === "awaiting_online";
}

/** The launch's own failure, as the copy a resume shows for it. */
function launchFailureMessage(launch: SandboxStatus): string {
  const reason = launch.error?.trim();
  return reason ? `The sandbox couldn't start: ${reason}` : "The sandbox couldn't start.";
}

/**
 * The next state for `event`. `undefined` is idle: nothing running and nothing
 * to report. Events that don't apply to the current attempt return `state` unchanged.
 */
export function reduceSandboxResume(
  state: SandboxResumeState | undefined,
  event: SandboxResumeEvent,
): SandboxResumeState | undefined {
  switch (event.type) {
    case "start": {
      if (isSandboxResumeActive(state)) return state;
      return {
        phase: "requesting",
        attempt: event.attempt,
        startedAt: event.now,
        progressAt: event.now,
        launchAtStart: sandboxLaunchKey(event.launch),
        lastLaunch: sandboxLaunchKey(event.launch),
        stageChanged: false,
        launchInProgress: event.launch !== null && !isSandboxLaunchStopped(event.launch),
      };
    }
    case "request_succeeded":
      if (state?.phase !== "requesting" || state.attempt !== event.attempt) return state;
      return { ...state, phase: "awaiting_online", progressAt: event.now };
    case "request_failed":
      if (state?.phase !== "requesting" || state.attempt !== event.attempt) return state;
      return { phase: "failed", attempt: state.attempt, message: event.message };
    case "host_online":
      // Liveness read before the server answered may still describe the sandbox
      // that stopped, so only an answered resume ends on it. A sandbox that
      // comes up anyway clears a reported failure.
      return state?.phase === "requesting" ? state : undefined;
    case "launch": {
      if (!isSandboxResumeActive(state)) return state;
      const key = sandboxLaunchKey(event.launch);
      const stageChanged = state.stageChanged || key !== state.launchAtStart;
      const launchInProgress = event.launch !== null && !isSandboxLaunchStopped(event.launch);
      if (stageChanged && event.launch !== null && isSandboxLaunchStopped(event.launch)) {
        // A stage waiting on the user explains itself in the launch banner.
        if (event.launch.stage === "action_required") return undefined;
        return {
          phase: "failed",
          attempt: state.attempt,
          message: launchFailureMessage(event.launch),
        };
      }
      return {
        ...state,
        stageChanged,
        launchInProgress,
        lastLaunch: key,
        progressAt: key !== state.lastLaunch ? event.now : state.progressAt,
      };
    }
    case "tick": {
      if (!isSandboxResumeActive(state)) return state;
      const overTotal = event.now - state.startedAt >= SANDBOX_RESUME_TOTAL_LIMIT_MS;
      // The server's answer can take as long as the launch; only the total limit applies until then.
      const quiet =
        state.phase === "awaiting_online" &&
        !state.launchInProgress &&
        event.now - state.progressAt >= SANDBOX_RESUME_QUIET_LIMIT_MS;
      if (!overTotal && !quiet) return state;
      return { phase: "failed", attempt: state.attempt, message: SANDBOX_RESUME_TIMED_OUT };
    }
  }
}
