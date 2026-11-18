import { useEffect } from "react";

import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { type AnyBlock, isTextBlock, type MessageContentBlock } from "@/lib/blocks";
import { splitSessionMessageOrigin } from "@/lib/sessionMessageOrigin";
import { isSystemUserContent } from "@/lib/systemMessage";
import { isStopPlanMessage } from "@/lib/workflow";
import { endPlanStop, settlePlanStop, usePlanStops, watchPlanStop } from "@/store/planStops";

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
 * to stop the plan: its key, else null. Sends the agent hasn't taken yet are
 * the latest.
 */
export function latestStopRequest(
  blocks: readonly AnyBlock[],
  pending: readonly { tempId: string; content: MessageContentBlock[] }[],
): string | null {
  const latestPending = pending.at(-1);
  if (latestPending) {
    return isStopPlanMessage(messageText(latestPending.content)) ? latestPending.tempId : null;
  }
  const latest = blocks.findLast(isUserRequest);
  if (latest?.type !== "user_message" || !isStopPlanMessage(messageText(latest.content))) {
    return null;
  }
  // A send the agent took keeps its pending key, so it stays the same message.
  return latest.stableKey ?? latest.ctx.itemId ?? "";
}

/** Whether the transcript shows another request from the user after the message *itemId*. */
export function hasRequestAfter(blocks: readonly AnyBlock[], itemId: string): boolean {
  const index = blocks.findLastIndex((block) => block.ctx.itemId === itemId);
  return index !== -1 && blocks.slice(index + 1).some(isUserRequest);
}

/**
 * Whether *sessionId*'s run is stopping: from the user's Stop until the session
 * stops working. A request the transcript shows after the Stop, as from
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
  const requestItemId = request?.itemId;
  const followed = useConversationEntryValue(
    sessionId,
    (state) => requestItemId !== undefined && hasRequestAfter(state.blocks, requestItemId),
  );
  useEffect(() => (sessionId === null ? undefined : watchPlanStop(sessionId)), [sessionId]);
  useEffect(() => {
    if (sessionId === null) return;
    if (request && followed) endPlanStop(sessionId, request);
    else if (!sessionWorking) settlePlanStop(sessionId, request, latestStop);
  }, [sessionId, sessionWorking, request, followed, latestStop]);
  const stopRequested = request !== undefined && !followed;
  if (stopRequested && !request.sent) return true;
  const stopIsLatest = latestStop !== null && latestStop !== settledStop;
  return sessionWorking && (stopRequested || stopIsLatest);
}
