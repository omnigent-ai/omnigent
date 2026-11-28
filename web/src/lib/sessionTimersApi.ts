/** The composer's `/loop` over the server's saved session timers: arm, stop, and send its runs. */

import { authenticatedFetch } from "./identity";
import { SIDE_CHAT_LABEL_KEY } from "./sessionListCache";
import { ApiError, apiErrorFromResponse, postEvent } from "./sessionsApi";

/** The `origin` the composer's `/loop` gives its timer and every message it sends. */
export const LOOP_TIMER_ORIGIN = "loop";

export interface SessionTimer {
  timerId: string;
  /** UTC ISO time of the first firing. */
  fireAt: string;
  intervalSeconds: number | null;
  /** For a loop, the caller's previous loop in the session that this one replaced. */
  replacedTimerId: string | null;
}

export interface SetSessionTimerInput {
  seconds: number;
  repeat: boolean;
  /** The prompt the loop repeats. */
  note: string;
  origin: string;
}

export const TIMER_NOT_SAVED_CODE = "timer_not_saved";
/** A session policy refused the message, so no turn ran. */
export const INPUT_DENIED_CODE = "input_denied";
/** The session's harness cannot start, so the message became a failed turn. */
export const INPUT_REJECTED_CODE = "input_rejected";

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
      note: input.note,
      origin: input.origin,
    }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as {
    durable?: boolean;
    reason?: string;
    timer_id?: string;
    fire_at?: string;
    interval_seconds?: number | null;
    replaced_timer_id?: string | null;
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
    replacedTimerId: body.replaced_timer_id ?? null,
  };
}

export async function cancelSessionTimer(sessionId: string, timerId: string): Promise<boolean> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/timers/${encodeURIComponent(timerId)}`,
    { method: "DELETE" },
  );
  if (res.status === 404) return false;
  if (!res.ok) throw await apiErrorFromResponse(res);
  return true;
}

async function deleteSessionLoop(
  sessionId: string,
): Promise<{ timer_id?: string | null; status?: string }> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/timers?origin=${LOOP_TIMER_ORIGIN}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as { timer_id?: string | null; status?: string };
}

/** Stop the caller's loop in the session: the cancelled timer's id, or `null` when none ran. */
export async function stopSessionLoop(sessionId: string): Promise<string | null> {
  const body = await deleteSessionLoop(sessionId);
  return typeof body.timer_id === "string" ? body.timer_id : null;
}

/**
 * Stop the caller's loop in a session they were moved on from. Resolves whether the
 * session had one: stopped by this call, or already retired there by the scheduler.
 */
export async function stopSupersededSessionLoop(sessionId: string): Promise<boolean> {
  const body = await deleteSessionLoop(sessionId);
  return typeof body.timer_id === "string" || body.status === "retired";
}

/**
 * Send a loop's prompt as the message every run of it sends: the first run from
 * the composer, then each tick from the server. Tagged so the transcript labels it.
 * Any refusal fails the run, an offline host included: nothing holds the message
 * for a resend. A policy denial or a harness that cannot start, which the server
 * reports on a successful reply, throws too.
 */
export async function sendLoopPrompt(sessionId: string, prompt: string): Promise<void> {
  const result = await postEvent(sessionId, {
    type: "message",
    data: {
      role: "user",
      origin: LOOP_TIMER_ORIGIN,
      content: [{ type: "input_text", text: prompt }],
    },
  });
  if (result.denied) {
    throw new ApiError("A session policy refused the prompt", 403, INPUT_DENIED_CODE);
  }
  if (result.rejected) {
    const reason = result.reason ? `: ${result.reason.replace(/\.$/, "")}` : "";
    throw new ApiError(`The session could not run the prompt${reason}`, 409, INPUT_REJECTED_CODE);
  }
}

const pendingMutations = new Map<string, Promise<unknown>>();

/**
 * Run *mutation* once every earlier one queued for *sessionId* has settled, so a
 * `/loop stop` waits for an arm still in flight. A rejected mutation doesn't block the next.
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
