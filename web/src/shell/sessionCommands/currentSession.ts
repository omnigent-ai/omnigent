// The session as the app knows it now, looked up by ID, so an action picked from a row
// drawn earlier acts on the session's current state, or not at all once it's gone.

import type { QueryClient } from "@tanstack/react-query";

import {
  type Conversation,
  isSessionDeleting,
  PINNED_CONVERSATIONS_KEY,
  type PinnedConversationsResult,
} from "@/hooks/useConversations";
import { findCachedListRow } from "@/lib/sessionListCache";

/**
 * The session's row in the sidebar's cached lists: the pinned section, the session lists,
 * project folders, or the open session's fetched row. Each is the cache's own object, so it
 * stays the same until the cache changes.
 */
function listedSessionRow(queryClient: QueryClient, id: string): Conversation | undefined {
  return (
    queryClient
      .getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY)
      ?.conversations.find((row) => row.id === id) ??
    findCachedListRow(queryClient, id) ??
    queryClient.getQueryData<Conversation | null>(["conversation-backfill", id]) ??
    undefined
  );
}

/** Whether the sidebar's lists hold the session, so leaving them means it's gone. */
export function isSessionListed(queryClient: QueryClient, id: string): boolean {
  return listedSessionRow(queryClient, id) !== undefined;
}

/**
 * `snapshot`'s session as the lists hold it now, or null once it's being deleted or was
 * removed. A session the lists held when `listed` was read is gone once they drop it; one
 * found only by searching keeps `snapshot`, with the owner and access its result reported.
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
  return listedSessionRow(queryClient, id) ?? (listed ? null : snapshot);
}
