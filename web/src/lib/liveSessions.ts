// Live (non-archived) sessions across all cached session lists (sidebar, folders, pins);
// the cache patchers keep them current, so archived or deleted sessions drop out.

import type { QueryClient } from "@tanstack/react-query";

import {
  type Conversation,
  PINNED_CONVERSATIONS_KEY,
  type PinnedConversationsResult,
} from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";

/** Every live session in the cached session lists, by id; the first cached row wins. */
export function liveSessionsInCache(queryClient: QueryClient): Map<string, Conversation> {
  const sessions = new Map<string, Conversation>();
  const addLive = (rows: readonly Conversation[]) => {
    for (const row of rows) {
      if (row.archived !== true && !sessions.has(row.id)) sessions.set(row.id, row);
    }
  };
  for (const queryKey of [["conversations"], ["project-sessions"]]) {
    for (const [, data] of queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey })) {
      for (const page of data?.pages ?? []) addLive(page.data);
    }
  }
  for (const [, data] of queryClient.getQueriesData<PinnedConversationsResult>({
    queryKey: PINNED_CONVERSATIONS_KEY,
  })) {
    addLive(data?.conversations ?? []);
  }
  return sessions;
}

/** Ids of every live session in the cached session lists. */
export function liveSessionIdsInCache(queryClient: QueryClient): Set<string> {
  return new Set(liveSessionsInCache(queryClient).keys());
}
