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
  adjustUnreadCount,
  applySuggestionUpdate,
  listSuggestions,
  locateSuggestion,
  reconcileSuggestion,
  restoreSuggestion,
  statusAfterUpdate,
  unreadDelta,
  updateSuggestion,
  type SuggestionPlace,
  type Suggestion,
  type SuggestionPage,
  type SuggestionStatus,
  type SuggestionUpdate,
} from "@/lib/proactiveInbox";
import { useRebasePath } from "@/lib/routing";
import { useProactiveAgentSettings } from "./useProactiveAgentSettings";
import { useViewerId } from "./useViewerId";

type Pages = InfiniteData<SuggestionPage, string | null>;
type Places = [readonly unknown[], SuggestionPlace][];

/** What an item with a pending update is known to be, apart from any one cache. */
interface TrackedStatus {
  /** The status the server last agreed to. */
  confirmed: SuggestionStatus;
  /** The status after the pending updates, which the unread totals reflect. */
  current: SuggestionStatus;
}

/**
 * In-flight update state for one query scope, shared by every instance of the
 * hook on the client (the Inbox page and the sidebar badge), so a poll that
 * lands mid-update reads the same overlay everywhere.
 */
interface ScopeState {
  /** The latest update per item whose PATCH has not settled, re-applied over fetched pages. */
  pending: Map<string, SuggestionUpdate>;
  inFlight: number;
  /** Each item's PATCHes go out one at a time, in the order the viewer made them. */
  chains: Map<string, Promise<unknown>>;
  /** Items the viewer resolved here; a later read or unread mark must not reopen them. */
  resolved: Set<string>;
  /**
   * Where each item with a pending update last stood in the cache with the
   * server's agreement, so a failed PATCH restores that state and not an earlier
   * optimistic one.
   */
  baselines: Map<string, Places>;
  /**
   * Each pending item's confirmed and optimistic status, so a list whose loaded
   * pages do not reach the item still moves its unread total the same way.
   */
  statuses: Map<string, TrackedStatus>;
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
      baselines: new Map(),
      statuses: new Map(),
      listeners: new Set(),
    };
    byKey.set(scopeKeyText, state);
  }
  return state;
}

function placesOf(queryClient: QueryClient, key: readonly unknown[], id: string): Places {
  const places: Places = [];
  for (const [queryKey, data] of queryClient.getQueriesData<Pages>({ queryKey: key })) {
    const place = data ? locateSuggestion(data.pages, id) : null;
    if (place) places.push([queryKey, place]);
  }
  return places;
}

/**
 * Agent-posted suggestions for the Inbox, when the proactive agent is on for the viewer.
 *
 * Updates apply optimistically and are overlaid on every fetched page until
 * their PATCH settles; the server's copy then replaces them, or the last
 * confirmed state comes back when the PATCH fails.
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
      scope.pending.set(id, change);
      scope.inFlight += 1;
      notify();
      void queryClient.cancelQueries({ queryKey: key });
      // The first unconfirmed change on an item records where it stood; later
      // changes build on that, so a failure never restores an optimistic state.
      if (!scope.baselines.has(id)) {
        const places = placesOf(queryClient, key, id);
        scope.baselines.set(id, places);
        const known = places[0]?.[1].item.status;
        if (known) scope.statuses.set(id, { confirmed: known, current: known });
      }
      const tracked = scope.statuses.get(id);
      queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
        data
          ? { ...data, pages: applySuggestionUpdate(data.pages, id, change, tracked?.current) }
          : data,
      );
      if (tracked) tracked.current = statusAfterUpdate(change);
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
          // A later change on the same item is still pending: the totals already
          // reflect it, so only the confirmed state moves here.
          const latest = scope.pending.get(id) === change;
          const status = scope.statuses.get(id);
          if (saved) {
            const copy = saved;
            queryClient.setQueriesData<Pages>({ queryKey: key }, (data) =>
              data
                ? {
                    ...data,
                    pages: reconcileSuggestion(
                      data.pages,
                      copy,
                      latest ? status?.current : undefined,
                    ),
                  }
                : data,
            );
            // The confirmed state is the server's copy where the item stood; the
            // cache may already carry a later unconfirmed change on top of it.
            scope.baselines.set(
              id,
              OPEN_SUGGESTION_STATUSES.includes(copy.status)
                ? (scope.baselines.get(id) ?? []).map(([queryKey, place]) => [
                    queryKey,
                    { ...place, item: copy },
                  ])
                : [],
            );
            if (status) {
              status.confirmed = copy.status;
              if (latest) status.current = copy.status;
            }
          } else {
            const places = scope.baselines.get(id) ?? [];
            const placed = new Set(places.map(([queryKey]) => JSON.stringify(queryKey)));
            for (const [queryKey, place] of places) {
              queryClient.setQueryData<Pages>(queryKey, (data) =>
                data ? { ...data, pages: restoreSuggestion(data.pages, place) } : data,
              );
            }
            if (latest && status) {
              // Lists that never held the item moved their totals by status alone.
              const delta = unreadDelta(status.current, status.confirmed);
              for (const [queryKey] of queryClient.getQueriesData<Pages>({ queryKey: key })) {
                if (placed.has(JSON.stringify(queryKey))) continue;
                queryClient.setQueryData<Pages>(queryKey, (data) =>
                  data ? { ...data, pages: adjustUnreadCount(data.pages, delta) } : data,
                );
              }
              status.current = status.confirmed;
            }
          }
        })
        .finally(() => {
          if (scope.pending.get(id) === change) {
            scope.pending.delete(id);
            scope.baselines.delete(id);
            scope.statuses.delete(id);
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
  // the confirmed status to the pending one.
  for (const [id, change] of state.pending) {
    pages = applySuggestionUpdate(pages, id, change, state.statuses.get(id)?.confirmed);
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
