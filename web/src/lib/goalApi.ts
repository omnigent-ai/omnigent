import { getClaudeGoal } from "./claudeGoalApi";
import { clearCodexGoal, getCodexGoal, setCodexGoal, updateCodexGoalStatus } from "./codexGoalApi";

/** Browser-facing goal state shared by goal-capable session backends. */
export interface Goal {
  threadId?: string;
  objective: string;
  status: string;
  tokenBudget: number | null;
  tokensUsed: number;
  timeUsedSeconds: number;
  createdAt: number | null;
  updatedAt: number | null;
  /** Latest goal check reason or pause notice, when the backend reports one. */
  statusDetail?: string | null;
  /** Goal checks the backend ran, when it reports them. */
  iterations?: number | null;
}

/** Session backends whose goal the composer can read. */
export type GoalBackend = "codex" | "claude";

export interface GoalResponse {
  goal: Goal | null;
}

export type GoalReadResponse = (GoalResponse & { available?: true }) | { available: false };

export interface SetGoalInput {
  objective: string;
  tokenBudget?: number | null;
  status?: GoalStatusUpdate | null;
}

export type GoalStatusUpdate = "active" | "paused";

export interface GoalReadOptions {
  /** Require a live runner response. Defaults to true. Codex only. */
  strict?: boolean;
  /** Backend that owns the session's goal. Defaults to Codex. */
  backend?: GoalBackend;
}

/**
 * Goal API facade used by the provider-neutral composer UI.
 *
 * Codex goals are read and written through its app-server. Claude Code goals
 * are read only: the UI changes them by sending Claude's `/goal` command.
 */
export async function getGoal(
  sessionId: string,
  options?: GoalReadOptions,
): Promise<GoalReadResponse> {
  if (options?.backend === "claude") return getClaudeGoal(sessionId);
  return getCodexGoal(sessionId, options);
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
