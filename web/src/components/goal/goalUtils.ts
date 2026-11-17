import type { Goal, GoalStatusUpdate } from "@/lib/goalApi";

export type GoalModeDraft = GoalStatusUpdate | "keep";

/**
 * Render the raw goal status.
 *
 * @param status - Goal status, e.g. ``"budgetLimited"``.
 * @returns The exact backend status string.
 */
export function formatGoalStatus(status: Goal["status"]): string {
  return status;
}

/** How a goal status reads at a glance: still running, waiting on the user, or finished. */
export type GoalStatusTone = "working" | "paused" | "done" | "failed";

const PAUSED_GOAL_STATUSES = new Set(["paused", "blocked", "usageLimited", "budgetLimited"]);

/**
 * Group a backend goal status by what the user needs to know about it.
 *
 * @param status - Goal status, e.g. ``"usageLimited"``.
 * @returns ``"paused"`` for any stopped-but-unfinished state, e.g. a usage limit.
 */
export function goalStatusTone(status: Goal["status"]): GoalStatusTone {
  if (status === "complete") return "done";
  if (status === "failed") return "failed";
  return PAUSED_GOAL_STATUSES.has(status) ? "paused" : "working";
}

const GOAL_STATUS_LABELS: Record<string, string> = {
  active: "Active",
  paused: "Paused",
  blocked: "Blocked",
  usageLimited: "Usage limited",
  budgetLimited: "Budget limited",
  complete: "Complete",
  failed: "Failed",
};

/**
 * Human-readable goal status.
 *
 * @param status - Goal status, e.g. ``"budgetLimited"``.
 * @returns A label such as ``"Budget limited"``, or the raw status when unknown.
 */
export function goalStatusLabel(status: Goal["status"]): string {
  return GOAL_STATUS_LABELS[status] ?? status;
}

export function canPauseGoal(goal: Goal | null): boolean {
  return goal?.status === "active";
}

export function canResumeGoal(goal: Goal | null): boolean {
  return goal?.status === "paused" || goal?.status === "blocked" || goal?.status === "usageLimited";
}

export function isGoalUserMode(status: Goal["status"] | null | undefined): boolean {
  return status === "active" || status === "paused";
}

export function goalModeDraftForGoal(goal: Goal | null): GoalModeDraft {
  if (!goal) return "active";
  return isGoalUserMode(goal.status) ? (goal.status as GoalStatusUpdate) : "keep";
}

/**
 * Render token and elapsed-time usage for a goal.
 *
 * @param goal - Current goal state.
 * @returns Compact usage label, e.g. ``"1,200 / 40,000 tokens / 3 min"``.
 */
export function formatGoalUsage(goal: Goal): string {
  const tokenLabel =
    goal.tokenBudget == null
      ? `${goal.tokensUsed.toLocaleString()} tokens`
      : `${goal.tokensUsed.toLocaleString()} / ${goal.tokenBudget.toLocaleString()} tokens`;
  const minutes = Math.floor(goal.timeUsedSeconds / 60);
  if (minutes <= 0) return tokenLabel;
  return `${tokenLabel} / ${minutes.toLocaleString()} min`;
}
