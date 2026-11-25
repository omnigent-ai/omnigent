/** Saved session timers (`/v1/sessions/{id}/timers`): the server primitive behind `/loop`. */

import { authenticatedFetch } from "./identity";
import { SIDE_CHAT_LABEL_KEY } from "./sessionListCache";
import { ApiError, apiErrorFromResponse } from "./sessionsApi";

/** The `origin` the composer's `/loop` tags its timers with, so it cancels only those. */
export const LOOP_TIMER_ORIGIN = "loop";

export interface SessionTimer {
  timerId: string;
  /** UTC ISO-8601 time of the first firing. */
  fireAt: string;
  /** Seconds between firings for a repeating timer; `null` for a one-shot. */
  intervalSeconds: number | null;
  seconds: number;
  repeat: boolean;
  note: string | null;
  origin: string | null;
}

export interface SetSessionTimerInput {
  seconds: number;
  repeat: boolean;
  note?: string | null;
  /** A short lowercase tag naming the client that sets the timer, e.g. {@link LOOP_TIMER_ORIGIN}. */
  origin?: string | null;
}

/** One of the caller's saved timers in a session, as `GET /v1/sessions/{id}/timers` lists it. */
export interface SessionTimerSummary {
  timerId: string;
  origin: string | null;
  name: string;
  repeat: boolean;
  intervalSeconds: number;
  /** UTC ISO-8601 time of the next firing; `null` once a one-shot has fired. */
  nextRunAt: string | null;
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
      ...(input.origin ? { origin: input.origin } : {}),
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
    origin?: string | null;
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
    origin: body.origin ?? input.origin ?? null,
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

/** The caller's active saved timers in *sessionId*, oldest first. */
export async function listSessionTimers(sessionId: string): Promise<SessionTimerSummary[]> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/timers`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as {
    timers?: {
      timer_id: string;
      origin?: string | null;
      name?: string;
      repeat?: boolean;
      interval_seconds?: number;
      next_run_at?: string | null;
    }[];
  };
  return (body.timers ?? []).map((timer) => ({
    timerId: timer.timer_id,
    origin: timer.origin ?? null,
    name: timer.name ?? "",
    repeat: timer.repeat === true,
    intervalSeconds: timer.interval_seconds ?? 0,
    nextRunAt: timer.next_run_at ?? null,
  }));
}

/** The loops `/loop` armed in *sessionId*: the caller's repeating timers tagged {@link LOOP_TIMER_ORIGIN}. */
export async function listSessionLoops(sessionId: string): Promise<SessionTimerSummary[]> {
  const timers = await listSessionTimers(sessionId);
  return timers.filter((timer) => timer.origin === LOOP_TIMER_ORIGIN && timer.repeat);
}

const pendingMutations = new Map<string, Promise<unknown>>();

/**
 * Run *mutation* once every earlier one queued for *sessionId* has settled, so two
 * `/loop` submissions can't both see no loop and arm two, and a `/loop stop` sent
 * while an arm is in flight waits for it and then cancels it. A rejected mutation
 * doesn't block the next.
 */
export function runSessionTimerMutation<T>(
  sessionId: string,
  mutation: () => Promise<T>,
): Promise<T> {
  const previous = pendingMutations.get(sessionId) ?? Promise.resolve();
  const run = previous.then(mutation, mutation);
  const settled = run.then(
    () => undefined,
    () => undefined,
  );
  pendingMutations.set(sessionId, settled);
  void settled.then(() => {
    if (pendingMutations.get(sessionId) === settled) pendingMutations.delete(sessionId);
  });
  return run;
}
