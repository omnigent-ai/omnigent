/**
 * Saved session timers: the server primitive behind the composer's `/loop`.
 *
 * `POST /v1/sessions/{id}/timers` saves a timer as a hidden scheduled task of
 * kind `timer`, so it survives runner exits, idle shutdowns and server restarts;
 * each firing sends the session a hidden `[System: timer <id> fired]` message
 * carrying the note, which starts or joins a turn as a user message would. The
 * server advertises the feature as `session_timers` in `/v1/info`.
 */

import { authenticatedFetch } from "./identity";
import { listAllScheduledTasks, type ScheduledTask } from "./scheduledTasksApi";
import { SIDE_CHAT_LABEL_KEY } from "./sessionListCache";
import { ApiError, apiErrorFromResponse } from "./sessionsApi";

export interface SessionTimer {
  timerId: string;
  /** UTC ISO-8601 time of the first firing. */
  fireAt: string;
  /** Seconds between firings for a repeating timer; `null` for a one-shot. */
  intervalSeconds: number | null;
  seconds: number;
  repeat: boolean;
  note: string | null;
}

export interface SetSessionTimerInput {
  seconds: number;
  repeat: boolean;
  note?: string | null;
}

/** The `ApiError.code` for a timer the server would not save (for example, a side chat's). */
export const TIMER_NOT_SAVED_CODE = "timer_not_saved";

/**
 * Whether *session* can hold saved timers: a top-level session, not a sub-agent's
 * or a side chat, which run on another session's runner. An unknown session is
 * assumed top-level; the server confirms.
 */
export function sessionTakesSavedTimers(
  session:
    { parentSessionId?: string | null; labels?: Record<string, string> | null } | null | undefined,
): boolean {
  if (session == null) return true;
  return session.parentSessionId == null && session.labels?.[SIDE_CHAT_LABEL_KEY] === undefined;
}

/**
 * Save a timer that wakes *sessionId*.
 *
 * @throws ApiError with {@link TIMER_NOT_SAVED_CODE} when the server answers that
 *   it keeps no timers for this session, and the server's own error otherwise.
 */
export async function setSessionTimer(
  sessionId: string,
  input: SetSessionTimerInput,
): Promise<SessionTimer> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/timers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      seconds: input.seconds,
      repeat: input.repeat,
      note: input.note ?? null,
    }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as {
    durable?: boolean;
    reason?: string;
    timer_id?: string;
    fire_at?: string;
    interval_seconds?: number | null;
    seconds?: number;
    repeat?: boolean;
    note?: string | null;
  };
  if (body.durable !== true || typeof body.timer_id !== "string") {
    throw new ApiError(
      body.reason ?? "The server did not save the timer",
      res.status,
      TIMER_NOT_SAVED_CODE,
    );
  }
  return {
    timerId: body.timer_id,
    fireAt: body.fire_at ?? "",
    intervalSeconds: body.interval_seconds ?? null,
    seconds: body.seconds ?? input.seconds,
    repeat: body.repeat ?? input.repeat,
    note: body.note ?? input.note ?? null,
  };
}

/**
 * Cancel one of the session's saved timers.
 *
 * @returns `false` when the timer was already gone (cancelled or fired), which the
 *   server reports as 404.
 */
export async function cancelSessionTimer(sessionId: string, timerId: string): Promise<boolean> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/timers/${encodeURIComponent(timerId)}`,
    { method: "DELETE" },
  );
  if (res.status === 404) return false;
  if (!res.ok) throw await apiErrorFromResponse(res);
  return true;
}

/** Whether *task* is a repeating saved timer: the shape `/loop` creates. */
export function isRepeatingTimer(task: ScheduledTask): boolean {
  return (
    task.kind === "timer" &&
    task.state === "active" &&
    task.rrule !== null &&
    !/;COUNT=1$/.test(task.rrule)
  );
}

/**
 * The caller's repeating saved timers in *sessionId*, oldest first. The server
 * lists timers only when asked for every kind, so the Automations page never
 * shows them.
 */
export async function listSessionLoops(sessionId: string): Promise<ScheduledTask[]> {
  const tasks = await listAllScheduledTasks();
  return tasks.filter((task) => task.sessionId === sessionId && isRepeatingTimer(task));
}
