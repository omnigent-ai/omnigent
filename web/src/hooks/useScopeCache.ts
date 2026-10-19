import { useCallback, useEffect, useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { appendScopePage, refreshScopeWindow, type ScopeCacheData } from "@/lib/sidebarData";
import { filterSessionScope } from "@/lib/sessionVisibility";
import { sidebarConfig } from "@/lib/sidebarConfig";
import { useViewerId } from "./useViewerId";
import { isStaleCursorError } from "@/lib/staleCursor";
import {
  fetchConversationsPage,
  timeInitialConversationLoad,
  useConversations,
  type SessionSortBy,
} from "./useConversations";

export function useScopeCache(
  visibility: "mine" | "shared",
  refreshIntervalMs: number | false,
  enabled = true,
  maxRefreshSessions = sidebarConfig.maxRefreshSessions,
  entryKey: string | null = null,
  pageSize = sidebarConfig.sessionPageSize,
  sortBy: SessionSortBy = "updated_at",
) {
  const client = useQueryClient();
  const queryKey = useMemo(
    () => [
      "conversations",
      "",
      false,
      null,
      visibility,
      ...(sortBy === "created_at" ? [sortBy] : []),
    ],
    [visibility, sortBy],
  );
  const pendingPage = useRef<AbortController | null>(null);
  const pendingResult = useRef<Promise<void> | null>(null);
  const viewerId = useViewerId();
  const filterData = useCallback(
    (data: ScopeCacheData): ScopeCacheData => ({
      ...data,
      pages: data.pages.map((page) => ({
        ...page,
        data: filterSessionScope(page.data, visibility, viewerId),
      })),
    }),
    [visibility, viewerId],
  );
  const query = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      // Let an in-flight page extend the window before choosing the refresh size.
      if (pendingResult.current) await pendingResult.current.catch(() => {});
      signal.throwIfAborted();
      const current = client.getQueryData<ScopeCacheData>(queryKey);
      const limit = current
        ? Math.min(current.windowSize ?? pageSize, maxRefreshSessions)
        : pageSize;
      const page = await timeInitialConversationLoad(undefined, visibility, () =>
        fetchConversationsPage({
          searchQuery: "",
          includeArchived: false,
          visibility,
          queryClient: client,
          signal,
          limit,
          sortBy,
        }),
      );
      return refreshScopeWindow(client.getQueryData<ScopeCacheData>(queryKey), page, limit, sortBy);
    },
    select: filterData,
    staleTime: enabled ? 30_000 : "static",
    refetchInterval: enabled ? refreshIntervalMs : false,
    enabled,
  });
  const previousEntry = useRef({ enabled: false, entryKey });
  const { data, refetch: refreshHead } = query;
  useEffect(() => {
    const entering =
      enabled &&
      (!previousEntry.current.enabled ||
        (entryKey !== null && entryKey !== previousEntry.current.entryKey));
    previousEntry.current = { enabled, entryKey };
    if (!enabled) void client.cancelQueries({ queryKey, exact: true });
    else if (entering && data) {
      // Join an automatic entry fetch instead of cancelling and starting another.
      void refreshHead({ cancelRefetch: false });
    }
  }, [enabled, entryKey, data, client, queryKey, refreshHead]);
  const pagination = useMutation({
    mutationFn: async ({ controller }: { controller: AbortController }) => {
      let refreshing = client.getQueryCache().find({ queryKey, exact: true });
      while (refreshing?.state.fetchStatus === "fetching" && refreshing.promise) {
        // A replacement refresh can start while the previous one is being cancelled.
        // oxlint-disable-next-line no-await-in-loop
        await refreshing.promise.catch(() => {});
        if (controller.signal.aborted) return;
        refreshing = client.getQueryCache().find({ queryKey, exact: true });
      }
      if (controller.signal.aborted) return;
      const tail = client.getQueryData<ScopeCacheData>(queryKey)?.pages.at(-1);
      if (!tail?.has_more || !tail.last_id) return;
      const after = tail.last_id;
      const pending = (async () => {
        const page = await fetchConversationsPage({
          after,
          searchQuery: "",
          includeArchived: false,
          visibility,
          queryClient: client,
          signal: controller.signal,
          limit: pageSize,
          sortBy,
        });
        if (controller.signal.aborted) return;
        client.setQueryData<ScopeCacheData>(queryKey, (current) =>
          current ? appendScopePage(current, page, pageSize, sortBy) : current,
        );
      })();
      // Refresh waits only for a page request, never for a click queued behind it.
      pendingResult.current = pending;
      try {
        await pending;
      } finally {
        if (pendingPage.current === controller) pendingResult.current = null;
      }
    },
    onError: (error, { controller }) => {
      if (!controller.signal.aborted && isStaleCursorError(error)) {
        void client.resetQueries({ queryKey, exact: true });
      }
    },
  });
  useEffect(() => {
    return () => {
      pendingPage.current?.abort();
      pendingPage.current = null;
      pendingResult.current = null;
    };
  }, [enabled, visibility, sortBy]);

  const { mutateAsync } = pagination;
  const fetchNextPage = useCallback(async () => {
    const tail = client.getQueryData<ScopeCacheData>(queryKey)?.pages.at(-1);
    if (!enabled || pendingPage.current || !tail?.has_more || !tail.last_id) return;
    const controller = new AbortController();
    pendingPage.current = controller;
    try {
      await mutateAsync({ controller });
    } finally {
      if (pendingPage.current === controller) {
        pendingPage.current = null;
      }
    }
  }, [client, queryKey, enabled, mutateAsync]);
  const pageError =
    pagination.variables?.controller.signal.aborted || isStaleCursorError(pagination.error)
      ? null
      : pagination.error;
  const { isError: pageFailed, reset: resetPage } = pagination;
  const refetch = useCallback(() => {
    if (!enabled) return Promise.resolve();
    if (pageFailed) resetPage();
    return refreshHead();
  }, [enabled, pageFailed, resetPage, refreshHead]);
  return {
    ...query,
    refetch,
    fetchNextPage,
    hasNextPage: Boolean(query.data?.pages.at(-1)?.has_more),
    isFetching:
      enabled &&
      (query.isFetching ||
        (pagination.isPending && !pagination.variables?.controller.signal.aborted)),
    isFetchingNextPage:
      enabled && pagination.isPending && !pagination.variables?.controller.signal.aborted,
    error: query.error ?? pageError,
    isError: query.isError || pageError !== null,
  };
}

export function useArchivedSessions(enabled: boolean, sortBy: SessionSortBy = "updated_at") {
  const query = useConversations(
    "",
    false,
    { enabled, snapshot: true, sortBy },
    undefined,
    "archived",
  );
  const previousEntry = useRef({ enabled: false, sortBy });
  const { data, refetch } = query;
  useEffect(() => {
    const opening =
      enabled && (!previousEntry.current.enabled || sortBy !== previousEntry.current.sortBy);
    previousEntry.current = { enabled, sortBy };
    // The initial load is automatic; entries and sort changes refresh retained snapshots.
    if (opening && data) void refetch({ cancelRefetch: false });
  }, [enabled, sortBy, data, refetch]);
  return query;
}
