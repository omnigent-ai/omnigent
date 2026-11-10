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
  reconcileSuggestion,
  updateSuggestion,
  type Suggestion,
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
 * Updates apply optimistically. Fetches in flight when an update starts or
 * settles are cancelled, pending updates are re-applied over any page that
 * lands while they are in flight, the server's copy of the item is written into
 * the cache before the overlay is released, and the list refetches once the last
 * update settles. So a stale poll can't bring back a dismissed or read item,
 * even when the refetch fails.
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
  // The latest update per item whose PATCH has not settled, re-applied over fetched pages.
  const pending = useRef(new Map<string, SuggestionUpdate>());
  const inFlight = useRef(0);
  // Each item's PATCHes go out one at a time, in the order the viewer made them.
  const chains = useRef(new Map<string, Promise<unknown>>());
  // Items the viewer resolved here; a later read or unread mark must not reopen them.
  const resolved = useRef(new Set<string>());
  const [, setPendingVersion] = useState(0);

  const update = useCallback(
    (id: string, change: SuggestionUpdate) => {
      if ("state" in change && resolved.current.has(id)) return;
      if ("resolution" in change) {
        if (change.resolution === "none") resolved.current.delete(id);
        else resolved.current.add(id);
      }
      const key = JSON.parse(scopeKeyText) as unknown[];
      pending.current.set(id, change);
      inFlight.current += 1;
      setPendingVersion((v) => v + 1);
      void queryClient.cancelQueries({ queryKey: key });
      queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
        data ? { ...data, pages: applySuggestionUpdate(data.pages, id, change) } : data,
      );
      const previous = chains.current.get(id) ?? Promise.resolve();
      const request = previous
        .then(async () => {
          let saved: Suggestion | null = null;
          try {
            saved = await updateSuggestion(id, change);
          } catch {
            if ("resolution" in change && change.resolution !== "none") {
              resolved.current.delete(id);
            }
            showToast("Couldn’t save your response to this suggestion. Try again.");
          }
          // A fetch that started while the PATCH was in flight may carry the old
          // state; drop it, then write the server's copy before releasing the overlay.
          await queryClient.cancelQueries({ queryKey: key });
          if (saved) {
            const copy = saved;
            queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
              data ? { ...data, pages: reconcileSuggestion(data.pages, copy) } : data,
            );
          }
        })
        .finally(() => {
          if (pending.current.get(id) === change) pending.current.delete(id);
          if (chains.current.get(id) === request) chains.current.delete(id);
          inFlight.current -= 1;
          setPendingVersion((v) => v + 1);
          if (inFlight.current === 0) void queryClient.invalidateQueries({ queryKey: key });
        });
      chains.current.set(id, request);
    },
    [queryClient, scopeKeyText],
  );

  let pages = query.data?.pages ?? [];
  for (const [id, change] of pending.current) pages = applySuggestionUpdate(pages, id, change);
  const first = pages[0];
  const listedStates = new Set(states);
  return {
    // A read mark keeps an item in the cache; only list the states asked for.
    items: enabled
      ? pages.flatMap((page) => page.data).filter((item) => listedStates.has(item.state))
      : [],
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
