import { useEffect } from "react";

import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { type AnyBlock, isTextBlock, type MessageContentBlock } from "@/lib/blocks";
import { splitSessionMessageOrigin } from "@/lib/sessionMessageOrigin";
import { isSystemUserContent } from "@/lib/systemMessage";
import { isStopPlanMessage } from "@/lib/workflow";
import { settlePlanStop, usePlanStops } from "@/store/planStops";

function messageText(content: readonly MessageContentBlock[]): string {
  return content
    .filter(isTextBlock)
    .map((block) => block.text)
    .join("");
}

/**
 * Whether the latest message or command the user sent the session asks its main
 * agent to stop the plan. Runtime notices and other sessions' messages, such as
 * workers reporting back, don't count; sends the agent hasn't taken yet are the latest.
 */
export function latestRequestIsStop(
  blocks: readonly AnyBlock[],
  pending: readonly { content: MessageContentBlock[] }[],
): boolean {
  const latestPending = pending.at(-1);
  if (latestPending) return isStopPlanMessage(messageText(latestPending.content));
  const latest = blocks.findLast(
    (block) =>
      block.type === "slash_command" ||
      (block.type === "user_message" &&
        !isSystemUserContent(block.content) &&
        splitSessionMessageOrigin(messageText(block.content)) === null),
  );
  return latest?.type === "user_message" && isStopPlanMessage(messageText(latest.content));
}

/**
 * Whether *sessionId*'s run is stopping: from the user's Stop until the session
 * stops working. Without a Stop from this page, as after a reload, a transcript
 * this page streams stands in for it.
 *
 * @param sessionWorking - Whether the session works, as the surface's own controls read it.
 */
export function usePlanStopping(sessionId: string | null, sessionWorking: boolean): boolean {
  const request = usePlanStops((state) =>
    sessionId === null ? undefined : state.requests[sessionId],
  );
  const stopIsLatest = useConversationEntryValue(sessionId, (state) =>
    latestRequestIsStop(state.blocks, state.pendingUserMessages),
  );
  useEffect(() => {
    if (sessionId !== null && request?.sent && !sessionWorking) settlePlanStop(sessionId, request);
  }, [sessionId, request, sessionWorking]);
  if (request && !request.sent) return true;
  return sessionWorking && (request !== undefined || stopIsLatest);
}
