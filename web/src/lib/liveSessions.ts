// The sessions the client knows are live: every non-archived row in the cached
// session lists (sidebar scopes, project folders, pins) and the rows fetched for
// sessions opened outside them, whether or not the sidebar shows it. The cache
// patchers apply WS deltas, archives, deletes and lost access to all of these, so a
// removed session drops out here too.

import type { QueryClient } from "@tanstack/react-query";

import {
  type Conversation,
  PINNED_CONVERSATIONS_KEY,
  type PinnedConversationsResult,
} from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";

/** Every live session in the cached session lists and fetched rows, by id; a list's row wins. */
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
  // An older session opened by link, which no cached page holds; null once it's gone.
  for (const [, row] of queryClient.getQueriesData<Conversation | null>({
    queryKey: ["conversation-backfill"],
  })) {
    if (row) addLive([row]);
  }
  return sessions;
}

/** Ids of every live session in the cached session lists. */
export function liveSessionIdsInCache(queryClient: QueryClient): Set<string> {
  return new Set(liveSessionsInCache(queryClient).keys());
}
