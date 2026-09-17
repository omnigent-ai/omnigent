import { replaceEqualDeep } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { SessionFilter } from "@/lib/sessionFilterPreferences";
import { dedupeSessionRows, mergeScopeRows, sessionRowsPage } from "@/lib/sidebarData";
import {
  sidebarConfig,
  SidebarConfigContext,
  PinCapacityContext,
  type SidebarConfig,
} from "@/lib/sidebarConfig";
import { filterSessionScope, sessionVisibility } from "@/lib/sessionVisibility";
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

function useSidebarSources(config: SidebarConfig) {
  const [selectedView, registerView] = useState<SessionFilter | null>(null);
  const viewNeedsShared = selectedView === "all" || selectedView === "shared";
  const sharedActive = config.sharedAvailable && (viewNeedsShared || config.inboxIncludesShared);
  const pinsIncludeShared = config.sharedAvailable && config.pinsIncludeShared;
  const mine = useScopeCache(
    "mine",
    config.mineRefreshMs,
    true,
    config.maxRefreshSessions,
    selectedView === "mine" || selectedView === "all" ? selectedView : null,
    config.sessionPageSize,
  );
  const shared = useScopeCache(
    "shared",
    config.sharedRefreshMs,
    sharedActive,
    config.maxRefreshSessions,
    viewNeedsShared ? selectedView : null,
    config.sessionPageSize,
  );
  const pinned = usePinnedConversations(pinsIncludeShared, config.pinCap);
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
    () => (sharedActive ? (shared.data?.pages.flatMap((p) => p.data) ?? []) : []),
    [shared.data, sharedActive],
  );
  const pinnedRows = pinned.data?.conversations;
  const viewerId = getCurrentUserId();
  const loadedRows = useMemo(
    () =>
      dedupeSessionRows([
        ...(pinnedRows ?? []),
        ...(sharedActive
          ? [...folders.values()].flat()
          : filterSessionScope([...folders.values()].flat(), "mine", viewerId)),
        ...mineRows,
        ...sharedRows,
      ]).filter((row) => !row.archived),
    [mineRows, sharedRows, pinnedRows, folders, sharedActive, viewerId],
  );
  const inboxRows = useMemo(
    () =>
      config.inboxIncludesShared
        ? loadedRows
        : loadedRows.filter((row) => sessionVisibility(row, viewerId) === "mine"),
    [loadedRows, config.inboxIncludesShared, viewerId],
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
        sharedActive && shared.hasNextPage,
        mineCursor,
        sharedCursor,
      ),
    [
      mineRows,
      sharedRows,
      mine.hasNextPage,
      shared.hasNextPage,
      sharedActive,
      mineCursor,
      sharedCursor,
    ],
  );
  const hasNextPage = mine.hasNextPage || (sharedActive && shared.hasNextPage);
  const { fetchNextPage: fetchMinePage } = mine;
  const { fetchNextPage: fetchSharedPage } = shared;
  const fetchNextPage = useCallback(async () => {
    // A load is one action, even when both scopes have another page.
    await Promise.allSettled([
      ...(mine.hasNextPage ? [fetchMinePage()] : []),
      ...(sharedActive && shared.hasNextPage ? [fetchSharedPage()] : []),
    ]);
  }, [mine.hasNextPage, fetchMinePage, sharedActive, shared.hasNextPage, fetchSharedPage]);
  const allData = useMemo(
    () =>
      mine.data || (sharedActive && shared.data)
        ? { pages: [sessionRowsPage(merged.rows, hasNextPage)], pageParams: [undefined] }
        : undefined,
    [mine.data, shared.data, sharedActive, merged.rows, hasNextPage],
  );
  const all: SidebarListQuery = {
    data: allData,
    hasNextPage,
    fetchNextPage,
    refetch: () =>
      Promise.allSettled([mine.refetch(), ...(sharedActive ? [shared.refetch()] : [])]),
    isLoading: mine.isLoading || (sharedActive && shared.isLoading),
    isFetching: mine.isFetching || (sharedActive && shared.isFetching),
    isFetchingNextPage: mine.isFetchingNextPage || (sharedActive && shared.isFetchingNextPage),
    isError: mine.isError || (sharedActive && shared.isError),
    error: mine.error ?? (sharedActive ? shared.error : null),
  };
  const loadedData = useMemo(
    () =>
      mine.data !== undefined || (sharedActive && shared.data !== undefined)
        ? { pages: [sessionRowsPage(loadedRows)], pageParams: [undefined] }
        : undefined,
    [mine.data, shared.data, sharedActive, loadedRows],
  );
  return {
    config,
    sharedAvailable: config.sharedAvailable,
    sharedActive,
    selectedView,
    registerView,
    pinsIncludeShared,
    mine,
    shared,
    all,
    inbox: config.inboxIncludesShared ? all : mine,
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
}: {
  children: ReactNode;
  config?: SidebarConfig;
}) {
  const data = useSidebarSources(config);
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
  return { data, isLoading: data === undefined && all.isLoading };
}

/** Register the mounted sidebar's persisted selection; unmount releases its demand. */
export function useSidebarView(view: SessionFilter) {
  const { registerView } = useSidebarData();
  useLayoutEffect(() => {
    registerView(view);
    return () => registerView(null);
  }, [view, registerView]);
}
