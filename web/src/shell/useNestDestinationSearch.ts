// The nests a session can move into for a typed search, with the server's title search behind
// the loaded rows. Shared by the sidebar's Move session → Nests menu and ⌘K's nest picker, so
// both page through the same results and recover from the same failures.

import { type Conversation, useSessionSearch } from "@/hooks/useConversations";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";

import { nestDestinations } from "./nestDestinations";

export interface NestDestinationSearch {
  destinations: Conversation[];
  /** The server search is loading its first or a later page. */
  searching: boolean;
  /** The search results belong to the query on screen. */
  settled: boolean;
  /** The server has more results; a later page may hold nests the first one filtered out. */
  hasMore: boolean;
  /** The server search failed after its retries; `retry` runs it again. */
  failed: boolean;
  showMore: () => void;
  retry: () => void;
}

export function useNestDestinationSearch({
  conversation,
  loaded,
  search,
  enabled,
  viewerId,
}: {
  conversation: Conversation;
  loaded: readonly Conversation[];
  search: string;
  /** The server can move sessions into nests. */
  enabled: boolean;
  viewerId: string | null;
}): NestDestinationSearch {
  const query = useDebouncedValue(search.trim(), 300);
  const result = useSessionSearch(query, "title", {
    enabled,
    includeArchived: false,
    limit: 20,
  });
  const settled = query === search.trim();
  const destinations = enabled
    ? nestDestinations({
        conversation,
        loaded,
        searchHits: settled ? (result.data?.pages.flatMap((page) => page.data) ?? []) : [],
        search,
        viewerId,
      })
    : [];
  return {
    destinations,
    searching: result.isFetching,
    settled,
    hasMore: enabled && !result.isError && result.hasNextPage === true,
    failed: enabled && result.isError,
    showMore: () => void result.fetchNextPage(),
    retry: () => void result.refetch(),
  };
}
