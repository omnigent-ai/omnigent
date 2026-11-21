// The session as the app knows it now, looked up by ID, so an action picked from a row
// drawn earlier acts on the session's current state, or not at all once it's gone.

import type { QueryClient } from "@tanstack/react-query";

import {
  type Conversation,
  findCachedConversationRow,
  isSessionDeleting,
} from "@/hooks/useConversations";

/** Whether the sidebar's loaded lists hold the session, so leaving them means it's gone. */
export function isSessionListed(queryClient: QueryClient, id: string): boolean {
  return findCachedConversationRow(queryClient, id) !== undefined;
}

/**
 * `snapshot`'s session as cached now, or null once it's being deleted or was removed. A
 * session the lists held when `listed` was read is gone once they drop it; one found only
 * by searching keeps `snapshot`, as nothing newer is loaded.
 */
export function currentSessionRow(
  queryClient: QueryClient,
  snapshot: Conversation,
  listed: boolean,
): Conversation | null {
  const { id } = snapshot;
  if (isSessionDeleting(id)) return null;
  // A removal reported by the server leaves the open session's fetched row null.
  if (queryClient.getQueryData(["conversation-backfill", id]) === null) return null;
  return findCachedConversationRow(queryClient, id) ?? (listed ? null : snapshot);
}
