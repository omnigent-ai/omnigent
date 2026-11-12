// ⌘K's session data: recent sessions while the query is empty, then grouped
// server-side title and content search. Rendering stays in the palette.

import { defaultFilter } from "cmdk";
import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  type ConversationsPage,
  useConversations,
  useSessionSearch,
} from "@/hooks/useConversations";
import { rememberOpenSessionRow } from "@/hooks/useOpenSessionRow";
import { BOOT_PROBE_TIMEOUT_MS } from "@/lib/bootCapabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { conversationDisplayLabel, getConversationAgentType } from "./sidebarNav";

/** Debounce matches the sidebar search (300ms) so keystrokes don't each fetch. */
const SEARCH_DEBOUNCE_MS = 300;
/** How many recent sessions to show before the user types, so the Actions
    group stays visible without scrolling. Typing lifts the cap. */
const IDLE_SESSION_LIMIT = 5;
const SESSION_SEARCH_PAGE_BATCH = 10;
const SESSION_SEARCH_RESULT_LIMIT = 50;
/** Rows per page of each search group; "Show more" loads the next page. */
const SEARCH_GROUP_PAGE_SIZE = 8;

export interface PaletteSession {
  id: string;
  label: string;
  agent: string;
  archived: boolean;
  snippet: string | null;
}

/** Flatten list pages into palette rows, dropping repeated ids and, unless
    `includeArchived`, archived sessions. `newestFirst` re-sorts the rows by
    `updated_at`, because pushed updates reorder sessions without a refetch. */
function paletteSessions(
  pages: readonly ConversationsPage[],
  {
    includeArchived = false,
    excludeIds = new Set(),
    newestFirst = false,
  }: { includeArchived?: boolean; excludeIds?: ReadonlySet<string>; newestFirst?: boolean } = {},
): PaletteSession[] {
  const seen = new Set(excludeIds);
  const out: PaletteSession[] = [];
  const rows = pages.flatMap((page) => page.data);
  // Stable, so rows with the same timestamp keep the server's order.
  if (newestFirst) rows.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
  for (const c of rows) {
    if ((c.archived && !includeArchived) || seen.has(c.id)) continue;
    seen.add(c.id);
    out.push({
      id: c.id,
      label: conversationDisplayLabel(c),
      agent: getConversationAgentType(c),
      archived: c.archived === true,
      // Present only when the match was in chat content (not the title);
      // the server omits it otherwise. Shown as a dimmed second line.
      snippet: c.search_snippet ?? null,
    });
  }
  return out;
}

export function usePaletteSessionSearch({
  open,
  sessionsOnly,
}: {
  open: boolean;
  sessionsOnly: boolean;
}) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [pageLimit, setPageLimit] = useState(SESSION_SEARCH_PAGE_BATCH);

  // Reset the query when the palette closes so it reopens clean.
  useEffect(() => {
    if (!open) {
      setQuery("");
      setDebouncedQuery("");
      setPageLimit(SESSION_SEARCH_PAGE_BATCH);
    }
  }, [open]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const { data, isFetching, isError, refetch, hasNextPage, fetchNextPage, isFetchNextPageError } =
    useConversations("", false, { enabled: open });
  const serverInfo = useServerInfo();
  // Older servers ignore `search_scope`, so they get one mixed group instead.
  const groupedSearch = serverInfo !== "loading" && serverInfo.session_search_scope === true;
  const searching = !sessionsOnly && debouncedQuery !== "";
  // Wait briefly for server info so a supporting server never gets the mixed
  // request; if it stalls, fall back to that request rather than never searching.
  const serverInfoLoading = serverInfo === "loading";
  const [serverInfoWaitExpired, setServerInfoWaitExpired] = useState(false);
  useEffect(() => {
    if (!serverInfoLoading || !searching || serverInfoWaitExpired) return;
    const timer = setTimeout(() => setServerInfoWaitExpired(true), BOOT_PROBE_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [serverInfoLoading, searching, serverInfoWaitExpired]);
  const awaitingServerInfo = serverInfoLoading && !serverInfoWaitExpired;
  const searchOptions = {
    enabled: open && searching && !awaitingServerInfo,
    limit: SEARCH_GROUP_PAGE_SIZE,
  };
  const titleSearch = useSessionSearch(
    debouncedQuery,
    groupedSearch ? "title" : "all",
    searchOptions,
  );
  const contentSearch = useSessionSearch(debouncedQuery, "content", {
    ...searchOptions,
    enabled: searchOptions.enabled && groupedSearch,
  });
  const loadedPages = data?.pages.length ?? 0;
  useEffect(() => {
    if (
      open &&
      sessionsOnly &&
      hasNextPage &&
      loadedPages < pageLimit &&
      !isFetching &&
      !isError &&
      !isFetchNextPageError
    ) {
      void fetchNextPage();
    }
  }, [
    open,
    sessionsOnly,
    hasNextPage,
    loadedPages,
    pageLimit,
    isFetching,
    isError,
    isFetchNextPageError,
    fetchNextPage,
  ]);

  const sessions = useMemo(() => {
    if (searching) return paletteSessions(titleSearch.data?.pages ?? [], { includeArchived: true });
    // Rows archived after they loaded are still cached here, so drop them.
    const out = paletteSessions(data?.pages ?? [], { newestFirst: true });
    if (sessionsOnly) {
      if (!query.trim()) return out;
      return out
        .map((session) => ({ ...session, score: defaultFilter(session.label, query.trim()) }))
        .filter((session) => session.score > 0)
        .sort((first, second) => second.score - first.score);
    }
    // The full first page would push Actions below the fold.
    return out.slice(0, IDLE_SESSION_LIMIT);
  }, [data, titleSearch.data, query, searching, sessionsOnly]);
  // Excludes title matches again in case a rename moved a session between the
  // two groups' requests: list rows need unique ids.
  const contentSessions = useMemo(
    () =>
      searching
        ? paletteSessions(contentSearch.data?.pages ?? [], {
            includeArchived: true,
            excludeIds: new Set(sessions.map((s) => s.id)),
          })
        : [],
    [contentSearch.data, searching, sessions],
  );
  const visibleSessions = sessionsOnly ? sessions.slice(0, SESSION_SEARCH_RESULT_LIMIT) : sessions;
  const searchGroups = [titleSearch, contentSearch];
  const loadError = searching
    ? searchGroups.some((group) => group.isError)
    : isError || isFetchNextPageError;
  const loadMoreError = searching
    ? searchGroups.some((group) => group.isFetchNextPageError)
    : isFetchNextPageError;
  const searchFetching =
    (searching && awaitingServerInfo) || searchGroups.some((group) => group.isFetching);
  const retrying = searching
    ? searchGroups.some((group) => group.isError && group.isFetching)
    : isFetching;
  const retryLoad = (): void => {
    if (!searching) {
      void (isFetchNextPageError ? fetchNextPage() : refetch());
      return;
    }
    for (const group of searchGroups) {
      if (group.isFetchNextPageError) void group.fetchNextPage();
      else if (group.isError) void group.refetch();
    }
  };

  const queryClient = useQueryClient();
  // Search results are dropped when the palette closes; the sidebar lists
  // the opened session from this copy, which keeps its owner.
  const rememberSearchRow = (id: string): void => {
    const searchRow = [titleSearch.data, contentSearch.data]
      .flatMap((results) => results?.pages ?? [])
      .flatMap((page) => page.data)
      .find((row) => row.id === id);
    if (searchRow) rememberOpenSessionRow(queryClient, searchRow);
  };

  return {
    query,
    setQuery,
    debouncedQuery,
    searching,
    sessions,
    visibleSessions,
    contentSessions,
    titleSearch,
    contentSearch,
    isFetching,
    loadError,
    loadMoreError,
    searchFetching,
    retrying,
    retryLoad,
    /** Sessions-only paging stopped at its bound with more pages on the server. */
    olderSessionsAvailable: hasNextPage === true && loadedPages >= pageLimit,
    searchOlderSessions: (): void => setPageLimit(loadedPages + SESSION_SEARCH_PAGE_BATCH),
    rememberSearchRow,
  };
}
