import { useCallback, useEffect, useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { appendScopePage, refreshScopeWindow, type ScopeCacheData } from "@/lib/sidebarData";
import { filterSessionScope } from "@/lib/sessionVisibility";
import { sidebarConfig } from "@/lib/sidebarConfig";
import { getCurrentUserId } from "@/lib/identity";
import { isStaleCursorError } from "@/lib/staleCursor";
import {
  fetchConversationsPage,
  timeInitialConversationLoad,
  useConversations,
} from "./useConversations";

export function useScopeCache(
  visibility: "mine" | "shared",
  refreshIntervalMs: number,
  enabled = true,
  maxRefreshSessions = sidebarConfig.maxRefreshSessions,
) {
  const client = useQueryClient();
  const queryKey = useMemo(
    () => ["conversations", "", false, null, visibility] as const,
    [visibility],
  );
  const pendingPage = useRef<AbortController | null>(null);
  const pendingResult = useRef<Promise<void> | null>(null);
  const viewerId = getCurrentUserId();
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
      const limit = Math.min(current?.windowSize ?? 30, maxRefreshSessions);
      const page = await timeInitialConversationLoad(undefined, visibility, () =>
        fetchConversationsPage({
          searchQuery: "",
          includeArchived: false,
          visibility,
          queryClient: client,
          signal,
          limit,
        }),
      );
      return refreshScopeWindow(client.getQueryData<ScopeCacheData>(queryKey), page, limit);
    },
    select: filterData,
    staleTime: 30_000,
    refetchInterval: refreshIntervalMs,
    enabled,
  });
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
        });
        if (controller.signal.aborted) return;
        client.setQueryData<ScopeCacheData>(queryKey, (current) =>
          current ? appendScopePage(current, page) : current,
        );
      })();
      // Refresh waits only for a page request, never for a click queued behind it.
      pendingResult.current = pending;
      try {
        await pending;
      } finally {
        if (pendingResult.current === pending) pendingResult.current = null;
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
  }, [enabled, visibility]);

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
  const { refetch: refreshHead } = query;
  const { isError: pageFailed, reset: resetPage } = pagination;
  const refetch = useCallback(() => {
    if (pageFailed) resetPage();
    return refreshHead();
  }, [pageFailed, resetPage, refreshHead]);
  return {
    ...query,
    refetch,
    fetchNextPage,
    hasNextPage: Boolean(query.data?.pages.at(-1)?.has_more),
    isFetching: query.isFetching || pagination.isPending,
    isFetchingNextPage: pagination.isPending,
    error: query.error ?? pageError,
    isError: query.isError || pageError !== null,
  };
}

export function useArchivedSessions(enabled: boolean) {
  const query = useConversations("", false, { enabled, snapshot: true }, undefined, "archived");
  const wasEnabled = useRef(false);
  const { data, refetch } = query;
  useEffect(() => {
    const opening = enabled && !wasEnabled.current;
    wasEnabled.current = enabled;
    // The initial load is automatic; later entries refresh the retained snapshot.
    if (opening && data) void refetch({ cancelRefetch: false });
  }, [enabled, data, refetch]);
  return query;
}
