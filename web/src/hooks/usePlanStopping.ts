import { useEffect } from "react";

import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { type AnyBlock, isTextBlock, type MessageContentBlock } from "@/lib/blocks";
import { splitSessionMessageOrigin } from "@/lib/sessionMessageOrigin";
import { isSystemUserContent } from "@/lib/systemMessage";
import { isStopPlanMessage } from "@/lib/workflow";
import {
  endPlanStop,
  notePlanStopSeen,
  settlePlanStop,
  usePlanStops,
  watchPlanStop,
} from "@/store/planStops";

/** A send the composer shows before the agent takes it; `posted` once the session accepted it. */
interface PendingSend {
  tempId: string;
  content: MessageContentBlock[];
  posted?: boolean;
}

const NO_PENDING: readonly PendingSend[] = [];

function messageText(content: readonly MessageContentBlock[]): string {
  return content
    .filter(isTextBlock)
    .map((block) => block.text)
    .join("");
}

/**
 * A message or command the user sent the session, not a runtime notice or
 * another session's message, such as a worker reporting back.
 */
function isUserRequest(block: AnyBlock): boolean {
  return (
    block.type === "slash_command" ||
    (block.type === "user_message" &&
      !isSystemUserContent(block.content) &&
      splitSessionMessageOrigin(messageText(block.content)) === null)
  );
}

/**
 * The latest request the user sent the session, when it asks its main agent
 * to stop the plan: its key, null for another request, or undefined without
 * any. A send the session accepted but the agent hasn't taken yet is the
 * latest; one still on its way doesn't count.
 */
export function latestStopRequest(
  blocks: readonly AnyBlock[],
  pending: readonly PendingSend[],
): string | null | undefined {
  const latestPending = pending.findLast((send) => send.posted === true);
  if (latestPending) {
    return isStopPlanMessage(messageText(latestPending.content)) ? latestPending.tempId : null;
  }
  const latest = blocks.findLast(isUserRequest);
  if (latest === undefined) return undefined;
  if (latest.type !== "user_message" || !isStopPlanMessage(messageText(latest.content))) {
    return null;
  }
  // A send the agent took keeps its pending key, so it stays the same message.
  return latest.stableKey ?? latest.ctx.itemId ?? "";
}

/**
 * Whether *sessionId*'s run is stopping: from the user's Stop until the session
 * stops working. A request the transcript shows after a stop message, as from
 * another tab, ends it. Without a Stop from this page, as after a reload, a
 * transcript this page streams stands in for it, until the session is seen to stop.
 *
 * @param sessionWorking - Whether the session works, as the surface's own controls read it.
 */
export function usePlanStopping(sessionId: string | null, sessionWorking: boolean): boolean {
  const request = usePlanStops((state) =>
    sessionId === null ? undefined : state.requests[sessionId],
  );
  const settledStop = usePlanStops((state) =>
    sessionId === null ? undefined : state.settledStops[sessionId],
  );
  const latestStop = useConversationEntryValue(sessionId, (state) =>
    latestStopRequest(state.blocks, state.pendingUserMessages),
  );
  // The transcript's items keep the order the session took requests in; pending sends don't.
  const committedStop = useConversationEntryValue(sessionId, (state) =>
    latestStopRequest(state.blocks, NO_PENDING),
  );
  // A later request ends a Stop the transcript showed; a transcript without requests doesn't.
  const followed = request?.seen === true && committedStop === null;
  useEffect(() => (sessionId === null ? undefined : watchPlanStop(sessionId)), [sessionId]);
  useEffect(() => {
    if (sessionId === null) return;
    if (request && followed) endPlanStop(sessionId, request);
    else if (request && typeof committedStop === "string") notePlanStopSeen(sessionId, request);
    if (!sessionWorking) settlePlanStop(sessionId, request, latestStop ?? null);
  }, [sessionId, sessionWorking, request, followed, committedStop, latestStop]);
  const stopRequested = request !== undefined && !followed;
  if (stopRequested && !request.sent) return true;
  const stopIsLatest = typeof latestStop === "string" && latestStop !== settledStop;
  return sessionWorking && (stopRequested || stopIsLatest);
}
