// What a shell tab shows before its terminal can, from one place: the session's liveness and its
// sandbox's resume (which own those facts) and the session's shell list. The tab's body and its
// strip icon both read this, so they agree. Every wait here is bounded, so nothing spins forever.

import { SANDBOX_RESUME_TOTAL_LIMIT_MS, type SandboxResumeStatus } from "@/lib/sandboxResume";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";

/** How long a shell may wait to appear in the session's shell list before the tab offers Try Again. */
export const SHELL_LIST_LIMIT_MS = 20_000;

export const WAKE_TIMED_OUT = "Waking the sandbox took too long. Try again.";
// The tab adds what to do next, which depends on who is looking.
export const START_TIMED_OUT = "Starting the session took too long.";

export type TerminalTabConnection =
  /** The shell list has it: the terminal shows itself, including its own reconnects. */
  | { kind: "live" }
  | { kind: "connecting" }
  | { kind: "ended" }
  /** The shell list couldn't be read, or didn't show the shell in time; Try Again asks again. */
  | { kind: "unreachable" }
  /** The sandbox is asleep, waking, or failed to wake; shown with the shared Resume notice. */
  | { kind: "sandbox"; status: Exclude<SandboxResumeStatus, "none">; failure: string | null }
  | { kind: "host_offline" }
  /** Not host-bound, and its runner stopped: whoever started it relaunches it. */
  | { kind: "local_stranded" }
  /** The session's runner isn't running; Resume session starts it. */
  | { kind: "runner_asleep"; failure: string | null };

/** What a shell tab can be waiting for, each with its own limit. */
export type TerminalTabWait = "shell_list" | "starting" | "reported_stage";

export interface TerminalTabInputs {
  /** Whether the session's shell list has this shell: listed, missing for good, or unknown. */
  inventory: "live" | "gone" | "unknown";
  /** Reading the shell list failed, and no new request is on its way. */
  listFailed: boolean;
  liveness: SessionLiveness["kind"];
  /** The session's managed host can be resumed, so a stalled wake offers Resume sandbox. */
  hostResumable: boolean;
  sandbox: {
    /** The status every Resume surface shows, including a reported launch stage. */
    status: SandboxResumeStatus;
    /** This app's own resume only. */
    ownStatus: SandboxResumeStatus;
    failure: string | null;
  };
  /** How long, in ms, the shell has waited for what ``terminalTabWait`` names. */
  waited: number;
}

/** Whether the tab's icon spins for ``connection``. */
export function terminalTabSpins(connection: TerminalTabConnection): boolean {
  return (
    connection.kind === "connecting" ||
    (connection.kind === "sandbox" && connection.status === "resuming")
  );
}

/** What the shell is waiting for, which its connection times, or null while it isn't waiting. */
export function terminalTabWait(
  inputs: Omit<TerminalTabInputs, "waited" | "hostResumable">,
): TerminalTabWait | null {
  const { inventory, liveness, sandbox } = inputs;
  if (inventory === "live") return null;
  if (sandbox.status !== "none") {
    // A launch stage someone else reported can go stale; this app's own resume times itself.
    return sandbox.status === "resuming" && sandbox.ownStatus !== "resuming"
      ? "reported_stage"
      : null;
  }
  if (liveness === "starting") return "starting";
  if (liveness !== "online" && liveness !== "unknown") return null;
  return inventory === "unknown" && !inputs.listFailed ? "shell_list" : null;
}

/** How long, in ms, a shell may wait for ``wait`` before its tab stops connecting. */
export function terminalTabWaitLimit(wait: TerminalTabWait): number {
  return wait === "shell_list" ? SHELL_LIST_LIMIT_MS : SANDBOX_RESUME_TOTAL_LIMIT_MS;
}

export function terminalTabConnection(inputs: TerminalTabInputs): TerminalTabConnection {
  const { liveness, sandbox, waited } = inputs;
  if (inputs.inventory === "live") return { kind: "live" };
  if (sandbox.status !== "none") {
    if (
      terminalTabWait(inputs) === "reported_stage" &&
      waited >= terminalTabWaitLimit("reported_stage")
    ) {
      return { kind: "sandbox", status: "asleep", failure: null };
    }
    return { kind: "sandbox", status: sandbox.status, failure: sandbox.failure };
  }
  if (liveness === "host_offline") return { kind: "host_offline" };
  if (liveness === "local_stranded") return { kind: "local_stranded" };
  if (liveness === "runner_asleep") return { kind: "runner_asleep", failure: null };
  if (liveness === "starting") {
    if (waited < terminalTabWaitLimit("starting")) return { kind: "connecting" };
    return inputs.hostResumable
      ? { kind: "sandbox", status: "failed", failure: WAKE_TIMED_OUT }
      : { kind: "runner_asleep", failure: START_TIMED_OUT };
  }
  if (inputs.inventory === "gone") return { kind: "ended" };
  if (inputs.listFailed) return { kind: "unreachable" };
  // Before the list loads, or while a newly opened shell isn't in it yet.
  return waited >= terminalTabWaitLimit("shell_list")
    ? { kind: "unreachable" }
    : { kind: "connecting" };
}
