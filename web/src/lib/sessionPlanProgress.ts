// Where a session's plan stands, as the session list reports it, and the line
// its sidebar hover card shows for it: "3/5 · Open PRs for safe upgrades".

import { WAITING_FOR_YOU } from "@/lib/sessionJournal";

/** Where the step a plan's progress names stands. */
export type PlanProgressStepState = "needs_input" | "started" | "failed" | "next";

/** Mirrors the server's `SessionPlanProgress` on session list rows. */
export interface SessionPlanProgress {
  /** Steps in the session's current plan. */
  total: number;
  /** Steps whose newest status in the current run is each of these. */
  done?: number;
  failed?: number;
  started?: number;
  needs_input?: number;
  /**
   * The step the plan is on, as the chat's task preview picks it: one waiting
   * for the user, else one in progress, else the next, else a failure holding
   * up the rest. Absent once the plan is finished.
   */
  step?: { name: string; state: PlanProgressStepState } | null;
}

/**
 * Whether two plan progress values say the same. The session list leaves a
 * finished plan's step out and its live updates send it as null, so those match.
 */
export function samePlanProgress(
  left: SessionPlanProgress | null | undefined,
  right: SessionPlanProgress | null | undefined,
): boolean {
  const key = (progress: SessionPlanProgress | null | undefined) =>
    progress ? JSON.stringify({ ...progress, step: progress.step ?? null }) : null;
  return key(left) === key(right);
}

/** The parts of a card's plan line, which renders them in order. */
export interface PlanProgressLine {
  /** Finished steps, failed ones included, as the chat's task preview counts them. */
  finished: number;
  total: number;
  /** A word before the step's name, e.g. "Failed"; null for none. */
  lead: { text: string; tone: "waiting" | "failed" | "muted" } | null;
  /** The step's name, or how the plan finished: "All done", "Finished, 1 failed". */
  text: string;
  /** Other steps in the same state as the named one, e.g. 2 for "+2". */
  more: number;
}

/**
 * The line for a session whose plan has started: null while no step has a
 * status, such as a planning chat's proposal or a run whose first step hasn't
 * begun.
 *
 * @param promptWaiting - Whether a prompt in the session waits for the user. As
 *   in the chat's task preview, a step in progress then waits for you too, and a
 *   finished plan with a step still in progress shows nothing rather than "All
 *   done".
 */
export function planProgressLine(
  progress: SessionPlanProgress,
  promptWaiting: boolean,
): PlanProgressLine | null {
  const done = progress.done ?? 0;
  const failed = progress.failed ?? 0;
  const started = progress.started ?? 0;
  const needsInput = progress.needs_input ?? 0;
  if (progress.total <= 0 || done + failed + started + needsInput === 0) return null;
  const line = { finished: done + failed, total: progress.total, lead: null, more: 0 };
  const step = progress.step;
  if (!step) {
    if (promptWaiting && started > 0) return null;
    return { ...line, text: failed === 0 ? "All done" : `Finished, ${failed} failed` };
  }
  switch (step.state) {
    case "needs_input":
      return {
        ...line,
        lead: { text: WAITING_FOR_YOU, tone: "waiting" },
        text: step.name,
        more: Math.max(needsInput - 1, 0),
      };
    case "started":
      return {
        ...line,
        lead: promptWaiting ? { text: WAITING_FOR_YOU, tone: "waiting" } : null,
        text: step.name,
        more: Math.max(started - 1, 0),
      };
    case "failed":
      return { ...line, lead: { text: "Failed", tone: "failed" }, text: step.name };
    case "next":
      return { ...line, lead: { text: "Next", tone: "muted" }, text: step.name };
    default:
      // A state from a newer server still names its step.
      return { ...line, text: step.name };
  }
}
