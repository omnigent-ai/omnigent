import { useEffect } from "react";

import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import {
  endPlanStop,
  hasRequestAfter,
  latestStopRequest,
  settlePlanStop,
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
  const request = usePlanStops((state) =>
    sessionId === null ? undefined : state.requests[sessionId],
  );
  const settledStops = usePlanStops((state) =>
    sessionId === null ? NO_SETTLED_STOPS : (state.settledStops[sessionId] ?? NO_SETTLED_STOPS),
  );
  const latestStop = useConversationEntryValue(sessionId, (state) =>
    latestStopRequest(state.blocks, state.pendingUserMessages),
  );
  const stopItem = request?.itemId;
  const followed = useConversationEntryValue(
    sessionId,
    (state) => stopItem !== undefined && hasRequestAfter(state.blocks, stopItem),
  );
  useEffect(() => (sessionId === null ? undefined : watchPlanStop(sessionId)), [sessionId]);
  useEffect(() => {
    if (sessionId === null) return;
    if (request && followed) endPlanStop(sessionId, request);
    if (!sessionWorking) {
      settlePlanStop(sessionId, request, typeof latestStop === "string" ? latestStop : null);
    }
  }, [sessionId, sessionWorking, request, followed, latestStop]);
  const stopRequested = request !== undefined && !followed;
  if (stopRequested && !request.sent) return true;
  const stopIsLatest = typeof latestStop === "string" && !settledStops.includes(latestStop);
  return sessionWorking && (stopRequested || stopIsLatest);
}
