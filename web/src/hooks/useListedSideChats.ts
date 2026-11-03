import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listSideChats } from "@/lib/sessionsApi";
import { isTempConvId } from "@/lib/tempConversationId";

/** Query key of a session's server-listed side chats. */
export function listedSideChatsQueryKey(conversationId: string) {
  return ["conversation", conversationId, "side_chats"] as const;
}

/**
 * The ids of the user's generic side chats the server lists on a session,
 * oldest first, or null until they load or when the server doesn't list side
 * chats. Refetched when the window regains focus, so side chats opened or
 * closed on another device show up on return.
 *
 * @param conversationId The parent session, e.g. "conv_abc123".
 */
export function useListedSideChats(conversationId: string): string[] | null {
  const sessionId = isTempConvId(conversationId) ? null : conversationId;
  const { data } = useQuery({
    queryKey: listedSideChatsQueryKey(sessionId ?? ""),
    queryFn: () => listSideChats(sessionId as string),
    enabled: sessionId !== null,
    refetchOnWindowFocus: "always",
  });
  return useMemo(() => data?.map((sideChat) => sideChat.id) ?? null, [data]);
}
