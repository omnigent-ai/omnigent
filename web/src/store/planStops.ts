import { create } from "zustand";

/** A Stop the user asked of a run's main agent; `sent` once its session took the message. */
export interface PlanStopRequest {
  /** Tells this Stop from a later one, across its `sent` update. */
  id: number;
  sent: boolean;
  /** The stop message's conversation item, once the session took it, when the server names it. */
  itemId?: string;
}

/** How delivering a stop message went: null when the session didn't take it. */
export type PlanStopDelivery = { itemId?: string } | null;

interface PlanStopsState {
  requests: Readonly<Partial<Record<string, PlanStopRequest>>>;
  /** Per session, the transcript's latest stop message once its run was seen to stop. */
  settledStops: Readonly<Partial<Record<string, string>>>;
}

/**
 * Per session, the Stop the user asked of its run's main agent, kept until the
 * run stops, the message fails or the session takes another request. The
 * Tasks tab and the run rows all read it, so they agree the run is stopping.
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
 * @param send Delivers the stop message.
 */
export async function trackPlanStop(
  sessionId: string,
  send: () => Promise<PlanStopDelivery>,
): Promise<void> {
  const sending: PlanStopRequest = { id: nextRequestId++, sent: false };
  replaceRequest(sessionId, currentRequest(sessionId), sending);
  let delivery: PlanStopDelivery = null;
  try {
    delivery = await send();
  } finally {
    replaceRequest(
      sessionId,
      sending,
      delivery
        ? { ...sending, sent: true, ...(delivery.itemId ? { itemId: delivery.itemId } : {}) }
        : undefined,
    );
  }
}

/** End *request*, unless a later Stop took its place. */
export function endPlanStop(sessionId: string, request: PlanStopRequest): void {
  replaceRequest(sessionId, request, undefined);
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
    if (sessionId !== null && superseded) endPlanStop(sessionId, superseded);
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
  if (request?.sent) endPlanStop(sessionId, request);
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
