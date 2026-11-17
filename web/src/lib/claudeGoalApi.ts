import type { Goal, GoalReadResponse } from "./goalApi";
import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

const CLAUDE_GOAL_REQUEST_TIMEOUT_MS = 30_000;

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

/**
 * Read the `/goal` of a Claude-native session.
 *
 * @param sessionId - Session id, e.g. `"conv_abc123"`.
 * @returns The goal, or `{ available: false }` while the runner cannot read it.
 */
export async function getClaudeGoal(sessionId: string): Promise<GoalReadResponse> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CLAUDE_GOAL_REQUEST_TIMEOUT_MS);
  try {
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/claude_goal`,
      { signal: controller.signal },
    );
    if (!res.ok) {
      throw new ApiError(`Could not read Claude goal (${res.status})`, res.status, null);
    }
    const body = (await res.json()) as { goal?: unknown; available?: unknown };
    if (body.available !== true) return { available: false };
    if (body.goal === null) return { goal: null, available: true };
    if (!isClaudeGoalWire(body.goal)) return { available: false };
    return { goal: claudeGoalFromWire(body.goal), available: true };
  } finally {
    clearTimeout(timer);
  }
}
