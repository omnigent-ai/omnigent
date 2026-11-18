import { create } from "zustand";

/** A Stop the user asked of a run's main agent; `sent` once its session took the message. */
export interface PlanStopRequest {
  sent: boolean;
}

interface PlanStopsState {
  requests: Readonly<Partial<Record<string, PlanStopRequest>>>;
}

/**
 * Per session, the Stop the user asked of its run's main agent, kept until the
 * run stops, the message fails or the user sends the session something else.
 * The Tasks tab and the run rows all read it, so they agree the run is stopping.
 */
export const usePlanStops = create<PlanStopsState>(() => ({ requests: {} }));

/** Replace *sessionId*'s request, unless another already took the place of *current*. */
function replaceRequest(
  sessionId: string,
  current: PlanStopRequest | undefined,
  next: PlanStopRequest | undefined,
): void {
  usePlanStops.setState((state) => {
    if (state.requests[sessionId] !== current || current === next) return state;
    const others = Object.entries(state.requests).filter(([id]) => id !== sessionId);
    return { requests: Object.fromEntries(next ? [...others, [sessionId, next]] : others) };
  });
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
  const sending: PlanStopRequest = { sent: false };
  replaceRequest(sessionId, usePlanStops.getState().requests[sessionId], sending);
  let taken = false;
  try {
    taken = await send();
  } finally {
    // Another message may have ended this Stop while it was on its way.
    replaceRequest(sessionId, sending, taken ? { sent: true } : undefined);
  }
}

/** The session took another message from the user, so its run is no longer just stopping. */
export function clearPlanStop(sessionId: string): void {
  replaceRequest(sessionId, usePlanStops.getState().requests[sessionId], undefined);
}

/** The run stopped: its session stopped working after taking *request*. */
export function settlePlanStop(sessionId: string, request: PlanStopRequest): void {
  replaceRequest(sessionId, request, undefined);
}
