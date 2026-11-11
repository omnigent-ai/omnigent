import {
  useInfiniteQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { showToast } from "@/components/ui/toast";
import { withBasePath } from "@/lib/basePath";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import {
  OPEN_SUGGESTION_STATUSES,
  applySuggestionUpdate,
  listSuggestions,
  locateSuggestion,
  reconcileSuggestion,
  updateSuggestion,
  type SuggestionPage,
  type SuggestionStatus,
  type SuggestionUpdate,
  type SuggestionUpdateResult,
} from "@/lib/proactiveInbox";
import { useRebasePath } from "@/lib/routing";
import { useProactiveAgentSettings } from "./useProactiveAgentSettings";
import { useViewerId } from "./useViewerId";

type Pages = InfiniteData<SuggestionPage, string | null>;

/**
 * In-flight update state for one query scope, shared by every instance of the
 * hook on the client (the Inbox page and the sidebar badge), so a poll that
 * lands mid-update reads the same overlay everywhere.
 *
 * The query cache holds only what the server has sent. Pending updates are
 * applied once, at render time, over whatever the cache holds, so a page
 * fetched mid-update and an optimistic view can never both move a total.
 */
interface ScopeState {
  /** The latest update per item whose PATCH has not settled. */
  pending: Map<string, SuggestionUpdate>;
  inFlight: number;
  /** Each item's PATCHes go out one at a time, in the order the viewer made them. */
  chains: Map<string, Promise<unknown>>;
  /** Items the viewer resolved here; a later read or unread mark must not reopen them. */
  resolved: Set<string>;
  /**
   * The status the server last agreed to for each pending item, so a list
   * whose loaded pages never reach the item still moves its total the same way.
   */
  confirmed: Map<string, SuggestionStatus>;
  /**
   * The lists whose loaded pages held each pending item when its first change
   * was made. A fresh page from one of them that lacks the item was served
   * after the server applied the change, so its total already reflects it.
   */
  holders: Map<string, Set<string>>;
  /** Instances to re-render when the overlay changes. */
  listeners: Set<() => void>;
}

const scopes = new WeakMap<QueryClient, Map<string, ScopeState>>();

function scopeState(queryClient: QueryClient, scopeKeyText: string): ScopeState {
  let byKey = scopes.get(queryClient);
  if (!byKey) {
    byKey = new Map();
    scopes.set(queryClient, byKey);
  }
  let state = byKey.get(scopeKeyText);
  if (!state) {
    state = {
      pending: new Map(),
      inFlight: 0,
      chains: new Map(),
      resolved: new Set(),
      confirmed: new Map(),
      holders: new Map(),
      listeners: new Set(),
    };
    byKey.set(scopeKeyText, state);
  }
  return state;
}

/** The item's status in the cached lists that hold it, and which lists those are. */
function locateInCaches(
  queryClient: QueryClient,
  key: readonly unknown[],
  id: string,
): { status: SuggestionStatus | undefined; holders: Set<string> } {
  let status: SuggestionStatus | undefined;
  const holders = new Set<string>();
  for (const [queryKey, data] of queryClient.getQueriesData<Pages>({ queryKey: key })) {
    const place = data ? locateSuggestion(data.pages, id) : null;
    if (!place) continue;
    status ??= place.item.status;
    holders.add(JSON.stringify(queryKey));
  }
  return { status, holders };
}

/**
 * Agent-posted suggestions for the Inbox, when the proactive agent is on for the viewer.
 *
 * Updates show at once and stay overlaid on every fetched page until their
 * PATCH settles; the server's reply, with the viewer's unread total after the
 * change, then goes into the cache. When the PATCH fails the overlay simply
 * lifts and the cached state shows again.
 *
 * :param identityReady: Whether the viewer is known yet.
 * :param statuses: Which statuses to list, e.g. only `"unread"` for the Unread tab.
 */
export function useProactiveInbox(
  identityReady: boolean,
  statuses: readonly SuggestionStatus[] = OPEN_SUGGESTION_STATUSES,
) {
  const { settings } = useProactiveAgentSettings();
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
  const queryKey = [...scopeKey, [...statuses]];
  const enabled = identityReady && settings?.enabled === true;
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => listSuggestions(pageParam, statuses),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    enabled,
    staleTime: 60_000,
    // Suggestions arrive from scheduled runs, so a slow poll is enough.
    refetchInterval: 300_000,
    retry: 1,
  });
  const state = scopeState(queryClient, scopeKeyText);
  const [, setPendingVersion] = useState(0);
  useEffect(() => {
    const rerender = () => setPendingVersion((v) => v + 1);
    state.listeners.add(rerender);
    return () => {
      state.listeners.delete(rerender);
    };
  }, [state]);

  const update = useCallback(
    (id: string, change: SuggestionUpdate) => {
      const scope = scopeState(queryClient, scopeKeyText);
      const notify = () => scope.listeners.forEach((listener) => listener());
      if ("status" in change && scope.resolved.has(id)) return;
      if ("resolution" in change) {
        if (change.resolution === "none") scope.resolved.delete(id);
        else scope.resolved.add(id);
      }
      const key = JSON.parse(scopeKeyText) as unknown[];
      // The first unconfirmed change on an item records what the server last
      // agreed to and which lists hold the item; later changes build on that.
      if (!scope.holders.has(id)) {
        const found = locateInCaches(queryClient, key, id);
        if (found.status) scope.confirmed.set(id, found.status);
        scope.holders.set(id, found.holders);
      }
      scope.pending.set(id, change);
      scope.inFlight += 1;
      notify();
      void queryClient.cancelQueries({ queryKey: key });
      const previous = scope.chains.get(id) ?? Promise.resolve();
      const request = previous
        .then(async () => {
          let saved: SuggestionUpdateResult | null = null;
          try {
            saved = await updateSuggestion(id, change);
          } catch {
            if ("resolution" in change && change.resolution !== "none") {
              scope.resolved.delete(id);
            }
            showToast("Couldn’t save your response to this suggestion. Try again.");
          }
          // A fetch that started while the PATCH was in flight may carry the old
          // status; drop it, then write the server's reply before releasing the overlay.
          await queryClient.cancelQueries({ queryKey: key });
          if (saved) {
            const reply = saved;
            queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
              data ? { ...data, pages: reconcileSuggestion(data.pages, reply) } : data,
            );
            scope.confirmed.set(id, reply.item.status);
          }
        })
        .finally(() => {
          if (scope.pending.get(id) === change) {
            scope.pending.delete(id);
            scope.confirmed.delete(id);
            scope.holders.delete(id);
          }
          if (scope.chains.get(id) === request) scope.chains.delete(id);
          scope.inFlight -= 1;
          notify();
          if (scope.inFlight === 0) void queryClient.invalidateQueries({ queryKey: key });
        });
      scope.chains.set(id, request);
    },
    [queryClient, scopeKeyText],
  );

  let pages = query.data?.pages ?? [];
  // Fetched pages carry the server's totals, so the overlay moves them from
  // the confirmed status to the pending one, once. A list that held the item
  // and no longer does was served after the change; its total already moved.
  const ownKeyText = JSON.stringify(queryKey);
  for (const [id, change] of state.pending) {
    const from = state.holders.get(id)?.has(ownKeyText) ? undefined : state.confirmed.get(id);
    pages = applySuggestionUpdate(pages, id, change, from);
  }
  const first = pages[0];
  const listedStatuses = new Set(statuses);
  return {
    enabled,
    // A read mark keeps an item in the cache; only list the statuses asked for.
    items: enabled
      ? pages.flatMap((page) => page.data).filter((item) => listedStatuses.has(item.status))
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
