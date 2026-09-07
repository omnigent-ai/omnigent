import { clearCodexGoal, getCodexGoal, setCodexGoal, updateCodexGoalStatus } from "./codexGoalApi";

/** Browser-facing goal state shared by goal-capable session backends. */
export interface Goal {
  objective: string;
  status: string;
  tokenBudget: number | null;
  tokensUsed: number;
  timeUsedSeconds: number;
  createdAt: number | null;
  updatedAt: number | null;
}

export interface GoalResponse {
  goal: Goal | null;
}

export interface SetGoalInput {
  objective: string;
  tokenBudget?: number | null;
  status?: GoalStatusUpdate | null;
}

export type GoalStatusUpdate = "active" | "paused";

/**
 * Goal API facade used by the provider-neutral composer UI.
 *
 * Codex remains the only supported backend in this refactor. The generic
 * session endpoint can replace this delegation without changing UI callers.
 */
export async function getGoal(sessionId: string): Promise<GoalResponse> {
  return getCodexGoal(sessionId);
}

export async function setGoal(sessionId: string, goal: SetGoalInput): Promise<GoalResponse> {
  return setCodexGoal(sessionId, goal);
}

export async function updateGoalStatus(
  sessionId: string,
  status: GoalStatusUpdate,
): Promise<GoalResponse> {
  return updateCodexGoalStatus(sessionId, status);
}

export async function clearGoal(sessionId: string): Promise<{ cleared: boolean }> {
  return clearCodexGoal(sessionId);
}
