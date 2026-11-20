import { useEffect, useState } from "react";

import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import {
  endPlanStop,
  hasRequestAfter,
  isOpenStopLatest,
  newPlanStopObserver,
  reportPlanStopActivity,
  usePlanStops,
  watchPlanStop,
} from "@/store/planStops";

const NO_SETTLED_STOPS: readonly string[] = [];

/**
 * Whether *sessionId*'s run is stopping: from the user's Stop until the session
 * stops working. A request the transcript shows after the Stop's own message,
 * as from another tab, ends it. Without a Stop from this page, as after a
 * reload, a transcript this page streams stands in for it, until the session
 * is seen to stop or takes another request.
 *
 * @param sessionWorking - Whether the session works, as the surface's own controls read it.
 */
export function usePlanStopping(sessionId: string | null, sessionWorking: boolean): boolean {
  const [observer] = useState(newPlanStopObserver);
  const request = usePlanStops((state) =>
    sessionId === null ? undefined : state.requests[sessionId],
  );
  const settledStops = usePlanStops((state) =>
    sessionId === null ? NO_SETTLED_STOPS : (state.settledStops[sessionId] ?? NO_SETTLED_STOPS),
  );
  const stopIsLatest = useConversationEntryValue(sessionId, (state) =>
    isOpenStopLatest(state.blocks, state.pendingUserMessages, settledStops),
  );
  const stopItem = request?.itemId;
  const followed = useConversationEntryValue(
    sessionId,
    (state) => stopItem !== undefined && hasRequestAfter(state.blocks, stopItem),
  );
  useEffect(
    () => (sessionId === null ? undefined : watchPlanStop(sessionId, observer)),
    [sessionId, observer],
  );
  useEffect(() => {
    if (sessionId === null) return;
    if (request && followed) endPlanStop(sessionId, request);
    // Re-reported as a Stop or a stop message appears, so one that arrives while idle settles.
    reportPlanStopActivity(sessionId, observer, sessionWorking);
  }, [sessionId, observer, sessionWorking, request, followed, stopIsLatest]);
  const stopRequested = request !== undefined && !followed;
  if (stopRequested && !request.sent) return true;
  return sessionWorking && (stopRequested || stopIsLatest);
}
