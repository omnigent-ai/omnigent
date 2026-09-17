import { replaceEqualDeep } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { dedupeSessionRows, mergeScopeRows, sessionRowsPage } from "@/lib/sidebarData";
import {
  sidebarConfig,
  SidebarConfigContext,
  PinCapacityContext,
  type SidebarConfig,
} from "@/lib/sidebarConfig";
import { sessionVisibility } from "@/lib/sessionVisibility";
import { getCurrentUserId } from "@/lib/identity";
import { sumPendingApprovals } from "@/lib/inbox";
import { useCommentInbox } from "./useCommentInbox";
import {
  usePinnedConversations,
  type Conversation,
  type useConversations,
} from "./useConversations";
import { useScopeCache } from "./useScopeCache";

export type SidebarListQuery = Pick<
  ReturnType<typeof useConversations>,
  "data" | "error" | "isError" | "isLoading" | "isFetching" | "isFetchingNextPage" | "hasNextPage"
> & { fetchNextPage: () => unknown; refetch?: () => unknown };

function useSidebarSources(config: SidebarConfig, sharedEnabled: boolean) {
  const mine = useScopeCache("mine", config.mineRefreshMs, true, config.maxRefreshSessions);
  const shared = useScopeCache(
    "shared",
    config.sharedRefreshMs,
    sharedEnabled,
    config.maxRefreshSessions,
  );
  const pinned = usePinnedConversations(sharedEnabled, config.pinCap);
  const [folders, setFolders] = useState<Map<string, Conversation[]>>(() => new Map());
  const registerFolder = useCallback((name: string, rows: Conversation[] | null) => {
    setFolders((previous) => {
      if (rows === null && !previous.has(name)) return previous;
      if (rows !== null && replaceEqualDeep(previous.get(name), rows) === previous.get(name))
        return previous;
      const next = new Map(previous);
      if (rows === null) next.delete(name);
      else next.set(name, rows);
      return next;
    });
  }, []);
  const mineRows = useMemo(() => mine.data?.pages.flatMap((p) => p.data) ?? [], [mine.data]);
  const sharedRows = useMemo(
    () => (sharedEnabled ? (shared.data?.pages.flatMap((p) => p.data) ?? []) : []),
    [shared.data, sharedEnabled],
  );
  const pinnedRows = pinned.data?.conversations;
  const loadedRows = useMemo(
    () =>
      dedupeSessionRows([
        ...(pinnedRows ?? []),
        ...[...folders.values()].flat(),
        ...mineRows,
        ...sharedRows,
      ]).filter((row) => !row.archived),
    [mineRows, sharedRows, pinnedRows, folders],
  );
  const inboxRows = useMemo(
    () =>
      config.inboxIncludesShared
        ? loadedRows
        : loadedRows.filter((row) => sessionVisibility(row, getCurrentUserId()) === "mine"),
    [loadedRows, config.inboxIncludesShared],
  );
  const comments = useCommentInbox(inboxRows);
  const inboxCount = sumPendingApprovals(inboxRows) + comments.items.length;
  const watchedIds = useMemo(() => loadedRows.map((row) => row.id), [loadedRows]);
  const mineCursor = mine.data?.pages.at(-1)?.last_id;
  const sharedCursor = shared.data?.pages.at(-1)?.last_id;
  const merged = useMemo(
    () =>
      mergeScopeRows(
        mineRows,
        sharedRows,
        mine.hasNextPage,
        sharedEnabled && shared.hasNextPage,
        mineCursor,
        sharedCursor,
      ),
    [
      mineRows,
      sharedRows,
      mine.hasNextPage,
      shared.hasNextPage,
      sharedEnabled,
      mineCursor,
      sharedCursor,
    ],
  );
  const hasNextPage = mine.hasNextPage || (sharedEnabled && shared.hasNextPage);
  const { fetchNextPage: fetchMinePage } = mine;
  const { fetchNextPage: fetchSharedPage } = shared;
  const fetchNextPage = useCallback(async () => {
    // A load is one action, even when both scopes have another page.
    await Promise.allSettled([
      ...(mine.hasNextPage && !mine.isFetching ? [fetchMinePage()] : []),
      ...(sharedEnabled && shared.hasNextPage && !shared.isFetching ? [fetchSharedPage()] : []),
    ]);
  }, [
    mine.hasNextPage,
    mine.isFetching,
    fetchMinePage,
    sharedEnabled,
    shared.hasNextPage,
    shared.isFetching,
    fetchSharedPage,
  ]);
  const allData = useMemo(
    () =>
      mine.data || (sharedEnabled && shared.data)
        ? { pages: [sessionRowsPage(merged.rows, hasNextPage)], pageParams: [undefined] }
        : undefined,
    [mine.data, shared.data, sharedEnabled, merged.rows, hasNextPage],
  );
  const all: SidebarListQuery = {
    data: allData,
    hasNextPage,
    fetchNextPage,
    refetch: () =>
      Promise.allSettled([mine.refetch(), ...(sharedEnabled ? [shared.refetch()] : [])]),
    isLoading: mine.isLoading || (sharedEnabled && shared.isLoading),
    isFetching: mine.isFetching || (sharedEnabled && shared.isFetching),
    isFetchingNextPage: mine.isFetchingNextPage || (sharedEnabled && shared.isFetchingNextPage),
    isError: mine.isError || (sharedEnabled && shared.isError),
    error: mine.error ?? (sharedEnabled ? shared.error : null),
  };
  const loadedData = useMemo(
    () =>
      mine.data !== undefined && (!sharedEnabled || shared.data !== undefined)
        ? { pages: [sessionRowsPage(loadedRows)], pageParams: [undefined] }
        : undefined,
    [mine.data, shared.data, sharedEnabled, loadedRows],
  );
  return {
    config,
    sharedEnabled,
    mine,
    shared,
    all,
    pinned,
    loadedRows,
    loadedData,
    inboxRows,
    inboxCount,
    comments,
    watchedIds,
    registerFolder,
    watermark: merged.watermark,
  };
}

export const SidebarDataContext = createContext<ReturnType<typeof useSidebarSources> | null>(null);

export function SidebarDataProvider({
  children,
  config = sidebarConfig,
  sharedEnabled = config.sharedEnabledDefault,
}: {
  children: ReactNode;
  config?: SidebarConfig;
  sharedEnabled?: boolean;
}) {
  const data = useSidebarSources(config, sharedEnabled);
  return (
    <SidebarConfigContext.Provider value={config}>
      <SidebarDataContext.Provider value={data}>
        <PinCapacityContext.Provider
          value={(data.pinned.data?.conversations.length ?? 0) >= config.pinCap}
        >
          {children}
        </PinCapacityContext.Provider>
      </SidebarDataContext.Provider>
    </SidebarConfigContext.Provider>
  );
}

export function useSidebarData() {
  const value = useContext(SidebarDataContext);
  if (!value) throw new Error("SidebarDataProvider is required");
  return value;
}

/** Read shared rows without mounting another session-list query. */
export function useLoadedConversations() {
  const { loadedData: data, all } = useSidebarData();
  return { data, isLoading: all.isLoading };
}
