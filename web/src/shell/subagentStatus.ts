// Canonical status classification + dot palette shared by the Subagents
// list (SubagentsPanel) and the graph/tree view (SubagentsGraphView), so
// the two never drift. The list view is the source of truth; both views
// classify an agent's activity and color its status dot from here.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";

// Collapsed activity of an agent, shared by the main row (derived from the
// parent session's snapshot status) and the child rows (derived from
// ``busy`` + ``current_task_status``). ``awaiting`` = parked on an approval
// / input prompt and needs the user's attention. ``disconnected`` = the
// runner dropped its tunnel or exited; the task did NOT genuinely fail, so
// it renders a quiet, non-destructive grey dot rather than the red "Failed".
export type AgentActivity =
  | "launching"
  | "working"
  | "awaiting"
  | "done"
  | "failed"
  | "disconnected"
  | "idle"
  | "other";

export interface AgentStatus {
  activity: AgentActivity;
  /** Human label, shown inline for notable states and always in the tooltip. */
  label: string;
  /** Optional detail for the tooltip / accessible label. */
  details?: string;
}

// Error codes that mean "the runner went away", not "the task failed".
// ``runner_disconnected`` is published when the SSE relay's tunnel drops
// mid-stream; ``runner_failed_to_start`` when a bound runner reports an
// unexpected exit. Both are surfaced via ``last_task_error.code`` (child
// rows) / the session snapshot's ``lastTaskError.code`` (main row). A
// genuine task failure carries any other code (or none), so it still
// renders the red "Failed" pill.
const RUNNER_DISCONNECT_CODES = new Set(["runner_disconnected", "runner_failed_to_start"]);

export function isRunnerDisconnectCode(code: string | null | undefined): boolean {
  return code != null && RUNNER_DISCONNECT_CODES.has(code);
}

function firstErrorLine(message: string): string {
  const first = message
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
  return first ?? message;
}

// Dot color per dot-rendered state — the single source of truth for both
// views. Working uses the animated RunningDot and awaiting uses the "Needs
// response" tag, so both are excluded here. The quiet connected-but-not-
// working states (launching, idle, done) read in the blue --session-active
// hue, while the disconnected runner reads in the neutral grey
// --muted-foreground. Blue --session-active = session alive but not actively
// working; grey --muted-foreground = disconnected.
const DOT_TONE: Record<Exclude<AgentActivity, "working" | "awaiting">, string> = {
  // Blue, quiet — "done" is an expected outcome, so it reads as a subtle
  // (/55) blue dot rather than a loud green one.
  done: "bg-session-active/55",
  failed: "bg-destructive",
  // Grey, not destructive — a disconnect is a transient liveness loss, not a
  // task failure, so it reads distinctly from the red "Failed". Full-strength
  // neutral --muted-foreground (not the shared amber --warning) so the "Needs
  // response" badge keeps its amber and the dot stays notable.
  disconnected: "bg-muted-foreground",
  idle: "bg-session-active/55",
  launching: "bg-session-active/70",
  // Exception: the verbatim "other status" fallthrough stays neutral grey.
  other: "bg-muted-foreground/55",
};

/**
 * Map an agent activity to its status-dot color className. Shared by the list
 * and graph views so a given status renders an identical dot in both.
 * ``working`` and ``awaiting`` are rendered specially (RunningDot / badge) and
 * are excluded.
 */
export function activityDotClassName(
  activity: Exclude<AgentActivity, "working" | "awaiting">,
): string {
  return DOT_TONE[activity];
}

/**
 * Resolve a child session's display status.
 *
 * @param child - One child-session summary from the poll.
 * @returns The collapsed activity + its label, e.g.
 *   ``{ activity: "working", label: "Working" }``.
 */
export function childStatus(child: ChildSessionInfo): AgentStatus {
  // Awaiting input outranks ``busy``: a sub-agent parked on an
  // elicitation is still "running" its turn (the future is pending),
  // so checking ``busy`` first would hide the prompt behind a generic
  // "Working" pill — exactly the signal the user needs to act on.
  if (child.pending_elicitations_count > 0) {
    return { activity: "awaiting", label: "Needs response" };
  }
  // ``busy`` is the authoritative live flag (queued or in_progress);
  // ``current_task_status`` may be "launching", "completed", "failed",
  // "cancelled", or null when no task has run yet.
  if (child.current_task_status === "launching") {
    return { activity: "launching", label: "Launching" };
  }
  if (child.busy) return { activity: "working", label: "Working" };
  // A runner disconnect/exit is NOT a task failure — branch on the error
  // code before the generic failed paths so it renders the quiet grey
  // disconnected dot instead of the red "Failed" pill.
  if (isRunnerDisconnectCode(child.last_task_error?.code)) {
    return {
      activity: "disconnected",
      label: "Disconnected",
      details: child.last_task_error ? firstErrorLine(child.last_task_error.message) : undefined,
    };
  }
  if (child.last_task_error) {
    return {
      activity: "failed",
      label: "Failed",
      details: firstErrorLine(child.last_task_error.message),
    };
  }
  if (child.current_task_status === "failed") return { activity: "failed", label: "Failed" };
  if (child.current_task_status === "completed") return { activity: "done", label: "Done" };
  if (child.current_task_status) {
    return { activity: "other", label: child.current_task_status };
  }
  return { activity: "idle", label: "Idle" };
}

/**
 * Resolve the parent ("main"/root) session's display status from its snapshot.
 *
 * @param status - ``session.status`` from the snapshot, e.g. ``"running"``,
 *   or ``undefined`` while the snapshot is still loading.
 * @param lastTaskError - The snapshot's ``lastTaskError`` (code + message),
 *   used to tell a benign runner disconnect/exit apart from a real failure
 *   when ``status === "failed"``.
 * @returns The collapsed activity + its label.
 */
export function sessionStatus(
  status: string | undefined,
  lastTaskError?: { code: string; message: string } | null,
): AgentStatus {
  if (status === "launching") return { activity: "launching", label: "Launching" };
  if (status === "running") return { activity: "working", label: "Working" };
  if (status === "failed") {
    // A runner disconnect/exit collapses the snapshot to ``failed`` but
    // preserves the cause in ``lastTaskError.code`` — branch on it before
    // the generic failed path so it reads as a quiet grey disconnected dot,
    // not a real failure.
    if (isRunnerDisconnectCode(lastTaskError?.code)) {
      return {
        activity: "disconnected",
        label: "Disconnected",
        details: lastTaskError ? firstErrorLine(lastTaskError.message) : undefined,
      };
    }
    return { activity: "failed", label: "Failed" };
  }
  return { activity: "idle", label: "Idle" };
}
