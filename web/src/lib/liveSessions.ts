// Live (non-archived) sessions across all cached session lists (sidebar, folders, pins);
// the cache patchers keep them current, so archived or deleted sessions drop out.

import type { QueryCacheNotifyEvent, QueryClient } from "@tanstack/react-query";

import {
  type Conversation,
  PINNED_CONVERSATIONS_KEY,
  type PinnedConversationsResult,
} from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";

/** The query-key roots of the session lists, so a listener can ignore every other query. */
const SESSION_LIST_ROOTS: ReadonlySet<unknown> = new Set([
  "conversations",
  "project-sessions",
  PINNED_CONVERSATIONS_KEY[0],
]);

/** Calls `onChange` for cache events on the session lists only; returns the unsubscribe. */
export function subscribeToSessionLists(
  queryClient: QueryClient,
  onChange: () => void,
): () => void {
  return queryClient.getQueryCache().subscribe((event: QueryCacheNotifyEvent) => {
    if (SESSION_LIST_ROOTS.has(event.query.queryKey[0])) onChange();
  });
}

/** Every live session in the cached session lists, by id; the first cached row wins. */
export function liveSessionsInCache(queryClient: QueryClient): Map<string, Conversation> {
  const sessions = new Map<string, Conversation>();
  const addLive = (rows: readonly Conversation[]) => {
    for (const row of rows) {
      if (row.archived !== true && !sessions.has(row.id)) sessions.set(row.id, row);
    }
  };
  for (const queryKey of [["conversations"], ["project-sessions"]] as const) {
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
