import { requestGoalJson } from "./codexGoalApi";
import type { Goal, GoalReadResponse } from "./goalApi";
import { ApiError } from "./sessionsApi";

/** Wire goal from `GET /v1/sessions/{id}/claude_goal`, derived from Claude's transcript. */
interface ClaudeGoalWire {
  condition: string;
  /** `active`, `paused`, `complete` or `failed`. */
  status: string;
  /** Latest goal check reason or pause notice. */
  detail?: string | null;
  /** Goal checks Claude reported, set once the goal finishes. */
  iterations?: number | null;
  tokens?: number | null;
  duration_ms?: number | null;
  /** Unix seconds. */
  set_at?: number | null;
  updated_at?: number | null;
}

interface ClaudeGoalReadWire {
  goal: ClaudeGoalWire | null;
  available: boolean;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function claudeGoalFromWire(wire: ClaudeGoalWire): Goal {
  const durationMs = optionalNumber(wire.duration_ms);
  const setAt = optionalNumber(wire.set_at);
  const updatedAt = optionalNumber(wire.updated_at);
  return {
    objective: wire.condition,
    status: wire.status,
    statusDetail: typeof wire.detail === "string" && wire.detail ? wire.detail : null,
    iterations: optionalNumber(wire.iterations),
    tokenBudget: null,
    tokensUsed: optionalNumber(wire.tokens) ?? 0,
    timeUsedSeconds: durationMs === null ? 0 : Math.floor(durationMs / 1000),
    createdAt: setAt === null ? null : Math.floor(setAt),
    updatedAt: updatedAt === null ? null : Math.floor(updatedAt),
  };
}

function isClaudeGoalWire(value: unknown): value is ClaudeGoalWire {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const goal = value as Partial<ClaudeGoalWire>;
  return typeof goal.condition === "string" && typeof goal.status === "string";
}

function isClaudeGoalReadWire(value: unknown): value is ClaudeGoalReadWire {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const body = value as Partial<ClaudeGoalReadWire>;
  if (typeof body.available !== "boolean") return false;
  if (body.goal === null) return true;
  return body.available && isClaudeGoalWire(body.goal);
}

/**
 * Read the `/goal` of a Claude-native session.
 *
 * @param sessionId - Session id, e.g. `"conv_abc123"`.
 * @returns The goal, or `{ available: false }` while the runner cannot read it.
 * @throws ApiError for a failed, timed-out (504), or malformed (502) read.
 */
export async function getClaudeGoal(sessionId: string): Promise<GoalReadResponse> {
  const body = await requestGoalJson<unknown>(
    `/v1/sessions/${encodeURIComponent(sessionId)}/claude_goal`,
    {},
    "claude_goal_request_timeout",
  );
  if (!isClaudeGoalReadWire(body)) {
    throw new ApiError(
      "Could not read Claude goal: server returned a malformed response.",
      502,
      "claude_goal_failed",
    );
  }
  if (!body.available) return { available: false };
  return { goal: body.goal === null ? null : claudeGoalFromWire(body.goal), available: true };
}
