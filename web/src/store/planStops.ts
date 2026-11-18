import { create } from "zustand";

/** A Stop the user asked of a run's main agent; `sent` once its session took the message. */
export interface PlanStopRequest {
  /** Tells this Stop from a later one, across its `sent` update. */
  id: number;
  sent: boolean;
}

interface PlanStopsState {
  requests: Readonly<Partial<Record<string, PlanStopRequest>>>;
  /** Per session, the transcript's latest stop message once its run was seen to stop. */
  settledStops: Readonly<Partial<Record<string, string>>>;
}

/**
 * Per session, the Stop the user asked of its run's main agent, kept until the
 * run stops, the message fails or the session takes another message from the
 * user. The Tasks tab and the run rows all read it, so they agree the run is stopping.
 */
export const usePlanStops = create<PlanStopsState>(() => ({ requests: {}, settledStops: {} }));

let nextRequestId = 1;
const watcherCounts = new Map<string, number>();

/** Replace *sessionId*'s request, unless a later Stop already took the place of *current*. */
function replaceRequest(
  sessionId: string,
  current: PlanStopRequest | undefined,
  next: PlanStopRequest | undefined,
): void {
  usePlanStops.setState((state) => {
    if (state.requests[sessionId]?.id !== current?.id || current === next) return state;
    const others = Object.entries(state.requests).filter(([id]) => id !== sessionId);
    return { requests: Object.fromEntries(next ? [...others, [sessionId, next]] : others) };
  });
}

function currentRequest(sessionId: string): PlanStopRequest | undefined {
  return usePlanStops.getState().requests[sessionId];
}

/**
 * Show *sessionId*'s run as stopping while *send* delivers the Stop, then until
 * the run stops. A Stop the session doesn't take restores the Stop control.
 *
 * @param send Delivers the stop message, resolving to whether the session took it.
 */
export async function trackPlanStop(
  sessionId: string,
  send: () => Promise<boolean>,
): Promise<void> {
  const sending: PlanStopRequest = { id: nextRequestId++, sent: false };
  replaceRequest(sessionId, currentRequest(sessionId), sending);
  let taken = false;
  try {
    taken = await send();
  } finally {
    replaceRequest(sessionId, sending, taken ? { ...sending, sent: true } : undefined);
  }
}

/**
 * Note the Stop in progress as the user sends *sessionId* another message.
 *
 * @returns Ends that Stop once the session takes the message. A Stop asked for
 *   after the message was sent isn't affected.
 */
export function supersedePlanStop(sessionId: string | null): () => void {
  const superseded = sessionId === null ? undefined : currentRequest(sessionId);
  return () => {
    if (sessionId !== null && superseded) replaceRequest(sessionId, superseded, undefined);
  };
}

/**
 * The session stopped working: its sent Stop, if any, is done, and so is the
 * stop message the transcript last shows, so a later wake isn't stopping.
 *
 * @param stopMessage The key of the transcript's latest stop message, or null.
 */
export function settlePlanStop(
  sessionId: string,
  request: PlanStopRequest | undefined,
  stopMessage: string | null,
): void {
  if (request?.sent) replaceRequest(sessionId, request, undefined);
  if (stopMessage === null || usePlanStops.getState().settledStops[sessionId] === stopMessage) {
    return;
  }
  usePlanStops.setState((state) => ({
    settledStops: { ...state.settledStops, [sessionId]: stopMessage },
  }));
}

/**
 * Keep *sessionId*'s Stop while a surface watches the session. Once none does,
 * nothing would see the run stop, so the Stop is dropped; a transcript the page
 * streams stands in for it.
 *
 * @returns Stops watching.
 */
export function watchPlanStop(sessionId: string): () => void {
  watcherCounts.set(sessionId, (watcherCounts.get(sessionId) ?? 0) + 1);
  return () => {
    const remaining = (watcherCounts.get(sessionId) ?? 1) - 1;
    if (remaining > 0) {
      watcherCounts.set(sessionId, remaining);
      return;
    }
    watcherCounts.delete(sessionId);
    replaceRequest(sessionId, currentRequest(sessionId), undefined);
  };
}
