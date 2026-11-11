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
  type Suggestion,
  type SuggestionPage,
  type SuggestionStatus,
  type SuggestionUpdate,
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
 * The query cache holds only what the server has confirmed. Pending updates
 * are applied once, at render time, over whatever the cache holds, so a page
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
   * whose loaded pages do not reach the item still moves its total the same way.
   */
  confirmed: Map<string, SuggestionStatus>;
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
      listeners: new Set(),
    };
    byKey.set(scopeKeyText, state);
  }
  return state;
}

/** The item's status in whichever cached list holds it, if any. */
function cachedStatus(
  queryClient: QueryClient,
  key: readonly unknown[],
  id: string,
): SuggestionStatus | undefined {
  for (const [, data] of queryClient.getQueriesData<Pages>({ queryKey: key })) {
    const place = data ? locateSuggestion(data.pages, id) : null;
    if (place) return place.item.status;
  }
  return undefined;
}

/**
 * Agent-posted suggestions for the Inbox, when the proactive agent is on for the viewer.
 *
 * Updates show at once and stay overlaid on every fetched page until their
 * PATCH settles; the server's copy then goes into the cache, or, when the
 * PATCH fails, the overlay simply lifts and the confirmed state shows again.
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
  const enabled = identityReady && settings?.enabled === true;
  const query = useInfiniteQuery({
    queryKey: [...scopeKey, [...statuses]],
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
      // agreed to; lists that never held the item move their totals from it.
      if (!scope.confirmed.has(id)) {
        const known = cachedStatus(queryClient, key, id);
        if (known) scope.confirmed.set(id, known);
      }
      scope.pending.set(id, change);
      scope.inFlight += 1;
      notify();
      void queryClient.cancelQueries({ queryKey: key });
      const previous = scope.chains.get(id) ?? Promise.resolve();
      const request = previous
        .then(async () => {
          let saved: Suggestion | null = null;
          try {
            saved = await updateSuggestion(id, change);
          } catch {
            if ("resolution" in change && change.resolution !== "none") {
              scope.resolved.delete(id);
            }
            showToast("Couldn’t save your response to this suggestion. Try again.");
          }
          // A fetch that started while the PATCH was in flight may carry the old
          // status; drop it, then write the server's copy before releasing the overlay.
          await queryClient.cancelQueries({ queryKey: key });
          if (saved) {
            const copy = saved;
            const from = scope.confirmed.get(id);
            queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
              data ? { ...data, pages: reconcileSuggestion(data.pages, copy, from) } : data,
            );
            scope.confirmed.set(id, copy.status);
          }
        })
        .finally(() => {
          if (scope.pending.get(id) === change) {
            scope.pending.delete(id);
            scope.confirmed.delete(id);
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
  // the confirmed status to the pending one, once.
  for (const [id, change] of state.pending) {
    pages = applySuggestionUpdate(pages, id, change, state.confirmed.get(id));
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
