import { useQuery } from "@tanstack/react-query";
import { listSideChats } from "@/lib/sessionsApi";
import { isTempConvId } from "@/lib/tempConversationId";

/** Query key of a session's server-listed side chats. */
export function listedSideChatsQueryKey(conversationId: string) {
  return ["conversation", conversationId, "side_chats"] as const;
}

/** A session's listed side-chat ids, oldest first, and when they were requested. */
export interface ListedSideChats {
  ids: string[];
  /** When the list's request started (ms since epoch), to order it against other lists. */
  requestedAt: number;
}

/**
 * The user's generic side chats the server lists on a session, or null until
 * they load or when the server doesn't list side chats. Refetched when the
 * window regains focus, so side chats opened or closed on another device show
 * up on return.
 *
 * @param conversationId The parent session, e.g. "conv_abc123".
 */
export function useListedSideChats(conversationId: string): ListedSideChats | null {
  const sessionId = isTempConvId(conversationId) ? null : conversationId;
  const { data } = useQuery({
    queryKey: listedSideChatsQueryKey(sessionId ?? ""),
    queryFn: async () => {
      const requestedAt = Date.now();
      const sideChats = await listSideChats(sessionId as string);
      return sideChats && { ids: sideChats.map((sideChat) => sideChat.id), requestedAt };
    },
    enabled: sessionId !== null,
    refetchOnWindowFocus: "always",
  });
  return data ?? null;
}
