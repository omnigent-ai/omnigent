import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/**
 * Raw Codex app-server goal object forwarded by the AP route.
 * The route preserves Codex's snake_case field names and open-ended
 * status string; the UI converts this to `CodexGoal` at the API boundary.
 */
interface CodexGoalWire {
  /** Codex thread id that owns the goal. */
  thread_id: string;
  /** User-facing objective text currently stored in Codex. */
  objective: string;
  /**
   * Raw Codex goal status. Kept as `string` because Codex can return
   * statuses that the browser may display but must not write directly.
   */
  status: string;
  /** Optional token cap; `null` means the goal has no token budget. */
  token_budget?: number | null;
  /** Tokens Codex reports as consumed toward the current goal. */
  tokens_used: number;
  /** Wall-clock seconds Codex reports as spent on the current goal. */
  time_used_seconds: number;
  /** Codex-created timestamp, if the CLI version provides one. */
  created_at?: number | null;
  /** Codex-updated timestamp, if the CLI version provides one. */
  updated_at?: number | null;
}

/** Wire response for goal read/set/status endpoints. */
interface CodexGoalResponseWire {
  /** Current goal, or `null` when Codex has no goal for the thread. */
  goal: CodexGoalWire | null;
}

/**
 * Browser-facing Codex goal shape. This is the camelCase projection of
 * `CodexGoalWire`; callers should not depend on raw app-server field names.
 */
export interface CodexGoal {
  /** Codex thread id that owns the goal. */
  threadId: string;
  /** User-facing objective text currently stored in Codex. */
  objective: string;
  /**
   * Raw Codex goal status. May include Codex-owned states like `blocked`,
   * `usageLimited`, `budgetLimited`, or `complete`.
   */
  status: string;
  /** Optional token cap; `null` means the goal has no token budget. */
  tokenBudget: number | null;
  /** Tokens Codex reports as consumed toward the current goal. */
  tokensUsed: number;
  /** Wall-clock seconds Codex reports as spent on the current goal. */
  timeUsedSeconds: number;
  /** Codex-created timestamp, if the CLI version provides one. */
  createdAt: number | null;
  /** Codex-updated timestamp, if the CLI version provides one. */
  updatedAt: number | null;
}

/** API response for reading or mutating the current Codex goal. */
export interface CodexGoalResponse {
  /** Current goal, or `null` when Codex has no goal for the thread. */
  goal: CodexGoal | null;
}

/** Payload accepted by `PUT /v1/sessions/{id}/codex_goal`. */
export interface SetCodexGoalInput {
  /** New goal objective. The server rejects blank objectives. */
  objective: string;
  /** Optional token cap; `null` clears the token budget. */
  tokenBudget?: number | null;
  /**
   * Optional user-writeable status to apply with the goal update.
   * `null`/absent means preserve the current Codex status.
   */
  status?: CodexGoalStatusUpdate | null;
}

/**
 * Goal statuses the browser is allowed to write. Codex-owned terminal or
 * limiter states are read-only and stay represented by `CodexGoal.status`.
 */
export type CodexGoalStatusUpdate = "active" | "paused";

export async function codexGoalApiErrorFromResponse(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code: string | null = null;
  try {
    const body = (await res.json()) as {
      detail?: string;
      error?: string | { code?: string; message?: string };
    };
    if (typeof body.error === "string") {
      code = body.error;
      if (typeof body.detail === "string") message = body.detail;
    } else {
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    }
  } catch {
    // Non-JSON / empty body - keep the status-line fallback.
  }
  return new ApiError(message, res.status, code);
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await codexGoalApiErrorFromResponse(res);
  return (await res.json()) as T;
}

/**
 * Convert one Codex-goal wire object to the camelCase UI type.
 *
 * @param wire - Snake-case goal object returned by AP.
 * @returns CamelCase goal object for UI code.
 */
function codexGoalFromWire(wire: CodexGoalWire): CodexGoal {
  return {
    threadId: wire.thread_id,
    objective: wire.objective,
    status: wire.status,
    tokenBudget: wire.token_budget ?? null,
    tokensUsed: wire.tokens_used,
    timeUsedSeconds: wire.time_used_seconds,
    createdAt: wire.created_at ?? null,
    updatedAt: wire.updated_at ?? null,
  };
}

/**
 * Convert a Codex-goal response body to the UI type.
 *
 * @param wire - Snake-case AP response body.
 * @returns CamelCase goal response.
 */
function codexGoalResponseFromWire(wire: CodexGoalResponseWire): CodexGoalResponse {
  return {
    goal: wire.goal == null ? null : codexGoalFromWire(wire.goal),
  };
}

/**
 * Read the current Codex-native thread goal for a session.
 *
 * @param sessionId - Session identifier, e.g. ``"conv_abc123"``.
 * @returns The current goal, or ``goal: null`` when Codex has none.
 */
export async function getCodexGoal(sessionId: string): Promise<CodexGoalResponse> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/codex_goal`);
  return codexGoalResponseFromWire(await readJsonOrThrow<CodexGoalResponseWire>(res));
}

/**
 * Set or replace the Codex-native thread goal for a session.
 *
 * @param sessionId - Session identifier, e.g. ``"conv_abc123"``.
 * @param goal - Objective text, optional token budget, and optional mode.
 * ``tokenBudget: null`` clears the Codex budget. Omitting ``status`` leaves
 * Codex's current lifecycle state unchanged.
 * @returns Updated Codex goal state.
 */
export async function setCodexGoal(
  sessionId: string,
  goal: SetCodexGoalInput,
): Promise<CodexGoalResponse> {
  const body: Record<string, string | number | null> = {
    objective: goal.objective,
  };
  if (goal.tokenBudget !== undefined) {
    body.token_budget = goal.tokenBudget;
  }
  if (goal.status !== undefined) {
    body.status = goal.status;
  }
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/codex_goal`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return codexGoalResponseFromWire(await readJsonOrThrow<CodexGoalResponseWire>(res));
}

/**
 * Pause or resume the Codex-native thread goal for a session.
 *
 * @param sessionId - Session identifier, e.g. ``"conv_abc123"``.
 * @param status - Target Codex status: ``"paused"`` pauses and
 * ``"active"`` resumes.
 * @returns Updated Codex goal state.
 */
export async function updateCodexGoalStatus(
  sessionId: string,
  status: CodexGoalStatusUpdate,
): Promise<CodexGoalResponse> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/codex_goal/status`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
  return codexGoalResponseFromWire(await readJsonOrThrow<CodexGoalResponseWire>(res));
}

/**
 * Clear the Codex-native thread goal for a session.
 *
 * @param sessionId - Session identifier, e.g. ``"conv_abc123"``.
 * @returns Whether Codex removed an existing goal.
 */
export async function clearCodexGoal(sessionId: string): Promise<{ cleared: boolean }> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/codex_goal`, {
    method: "DELETE",
  });
  return readJsonOrThrow<{ cleared: boolean }>(res);
}
