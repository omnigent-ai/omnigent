import { useInfiniteQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { showToast } from "@/components/ui/toast";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { withBasePath } from "@/lib/basePath";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import {
  OPEN_SUGGESTION_STATES,
  applySuggestionUpdate,
  listSuggestions,
  updateSuggestion,
  type SuggestionPage,
  type SuggestionState,
  type SuggestionUpdate,
} from "@/lib/proactiveInbox";
import { useRebasePath } from "@/lib/routing";
import { useViewerId } from "./useViewerId";

type Pages = InfiniteData<SuggestionPage, string | null>;

/**
 * Agent-posted suggestions for the Inbox, when the server offers them.
 *
 * Updates apply optimistically. Fetches in flight when an update starts are
 * cancelled, pending updates are re-applied over any page that lands while
 * they are in flight, and the list refetches once the last one settles, so a
 * stale poll can't bring back a dismissed or read item.
 *
 * :param identityReady: Whether the viewer is known yet.
 * :param states: Which states to list, e.g. only `"unread"` for the Unread tab.
 */
export function useProactiveInbox(
  identityReady: boolean,
  states: readonly SuggestionState[] = OPEN_SUGGESTION_STATES,
) {
  const info = useServerInfo();
  const viewerId = useViewerId();
  const rebasePath = useRebasePath();
  const queryClient = useQueryClient();
  const scopeKey = [
    "proactive-inbox",
    getOmnigentServerIdentity() ?? `mount:${getOmnigentHostGeneration()}`,
    withBasePath(rebasePath("/")),
    viewerId,
  ];
  const scopeKeyText = JSON.stringify(scopeKey);
  const enabled = identityReady && info !== "loading" && info.proactive_agent_enabled === true;
  const query = useInfiniteQuery({
    queryKey: [...scopeKey, [...states]],
    queryFn: ({ pageParam }) => listSuggestions(pageParam, states),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    enabled,
    staleTime: 60_000,
    // Suggestions arrive from scheduled runs, so a slow poll is enough.
    refetchInterval: 300_000,
    retry: 1,
  });
  // Updates whose PATCH is still in flight, re-applied over fetched pages.
  const pending = useRef(new Map<string, SuggestionUpdate>());
  const [, setPendingVersion] = useState(0);

  const update = useCallback(
    (id: string, change: SuggestionUpdate) => {
      const key = JSON.parse(scopeKeyText) as unknown[];
      pending.current.set(id, change);
      setPendingVersion((v) => v + 1);
      void queryClient.cancelQueries({ queryKey: key });
      queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
        data ? { ...data, pages: applySuggestionUpdate(data.pages, id, change) } : data,
      );
      void updateSuggestion(id, change)
        .catch(() => {
          showToast("Couldn’t save your response to this suggestion. Try again.");
        })
        .finally(() => {
          if (pending.current.get(id) === change) pending.current.delete(id);
          setPendingVersion((v) => v + 1);
          if (pending.current.size === 0) void queryClient.invalidateQueries({ queryKey: key });
        });
    },
    [queryClient, scopeKeyText],
  );

  let pages = query.data?.pages ?? [];
  for (const [id, change] of pending.current) pages = applySuggestionUpdate(pages, id, change);
  const first = pages[0];
  return {
    items: enabled ? pages.flatMap((page) => page.data) : [],
    unreadCount: enabled ? (first?.unreadCount ?? 0) : 0,
    unreadCountCapped: enabled && first?.unreadCountCapped === true,
    isLoading: enabled && query.isLoading,
    isError: enabled && query.isError,
    hasNextPage: enabled && query.hasNextPage,
    isFetchingNextPage: query.isFetchingNextPage,
    fetchNextPage: query.fetchNextPage,
    refetch: query.refetch,
    update,
  };
}
