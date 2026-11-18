import { useEffect } from "react";

import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { type AnyBlock, isTextBlock, type MessageContentBlock } from "@/lib/blocks";
import { splitSessionMessageOrigin } from "@/lib/sessionMessageOrigin";
import { isSystemUserContent } from "@/lib/systemMessage";
import { isStopPlanMessage } from "@/lib/workflow";
import { settlePlanStop, usePlanStops, watchPlanStop } from "@/store/planStops";

function messageText(content: readonly MessageContentBlock[]): string {
  return content
    .filter(isTextBlock)
    .map((block) => block.text)
    .join("");
}

/**
 * The latest message or command the user sent the session, when it asks its
 * main agent to stop the plan: its key, else null. Runtime notices and other
 * sessions' messages, such as workers reporting back, don't count; sends the
 * agent hasn't taken yet are the latest.
 */
export function latestStopRequest(
  blocks: readonly AnyBlock[],
  pending: readonly { tempId: string; content: MessageContentBlock[] }[],
): string | null {
  const latestPending = pending.at(-1);
  if (latestPending) {
    return isStopPlanMessage(messageText(latestPending.content)) ? latestPending.tempId : null;
  }
  const latest = blocks.findLast(
    (block) =>
      block.type === "slash_command" ||
      (block.type === "user_message" &&
        !isSystemUserContent(block.content) &&
        splitSessionMessageOrigin(messageText(block.content)) === null),
  );
  if (latest?.type !== "user_message" || !isStopPlanMessage(messageText(latest.content))) {
    return null;
  }
  // A send the agent took keeps its pending key, so it stays the same message.
  return latest.stableKey ?? latest.ctx.itemId ?? "";
}

/**
 * Whether *sessionId*'s run is stopping: from the user's Stop until the session
 * stops working. Without a Stop from this page, as after a reload, a transcript
 * this page streams stands in for it, until the session is seen to stop.
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
  useEffect(() => (sessionId === null ? undefined : watchPlanStop(sessionId)), [sessionId]);
  useEffect(() => {
    if (sessionId !== null && !sessionWorking) settlePlanStop(sessionId, request, latestStop);
  }, [sessionId, sessionWorking, request, latestStop]);
  if (request && !request.sent) return true;
  const stopIsLatest = latestStop !== null && latestStop !== settledStop;
  return sessionWorking && (request !== undefined || stopIsLatest);
}
