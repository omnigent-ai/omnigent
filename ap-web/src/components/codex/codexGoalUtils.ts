import type { CodexGoal, CodexGoalStatusUpdate } from "@/lib/codexGoalApi";

export type CodexGoalModeDraft = CodexGoalStatusUpdate | "keep";

/**
 * Render the raw Codex goal status.
 *
 * @param status - Codex goal status, e.g. ``"budgetLimited"``.
 * @returns The exact Codex status string.
 */
export function formatCodexGoalStatus(status: CodexGoal["status"]): string {
  return status;
}

export function canPauseCodexGoal(goal: CodexGoal | null): boolean {
  return goal?.status === "active";
}

export function canResumeCodexGoal(goal: CodexGoal | null): boolean {
  return (
    goal?.status === "paused" ||
    goal?.status === "blocked" ||
    goal?.status === "usageLimited"
  );
}

export function isCodexGoalUserMode(status: CodexGoal["status"] | null | undefined): boolean {
  return status === "active" || status === "paused";
}

export function codexGoalModeDraftForGoal(goal: CodexGoal | null): CodexGoalModeDraft {
  if (!goal) return "active";
  return isCodexGoalUserMode(goal.status) ? (goal.status as CodexGoalStatusUpdate) : "keep";
}

/**
 * Render token and elapsed-time usage for a Codex goal.
 *
 * @param goal - Current Codex goal state.
 * @returns Compact usage label, e.g. ``"1,200 / 40,000 tokens / 3 min"``.
 */
export function formatCodexGoalUsage(goal: CodexGoal): string {
  const tokenLabel =
    goal.tokenBudget == null
      ? `${goal.tokensUsed.toLocaleString()} tokens`
      : `${goal.tokensUsed.toLocaleString()} / ${goal.tokenBudget.toLocaleString()} tokens`;
  const minutes = Math.floor(goal.timeUsedSeconds / 60);
  if (minutes <= 0) return tokenLabel;
  return `${tokenLabel} / ${minutes.toLocaleString()} min`;
}
