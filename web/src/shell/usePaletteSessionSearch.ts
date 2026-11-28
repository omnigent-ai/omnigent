// ⌘K's session data: recent sessions, archived included, while the query is
// empty, then grouped server-side title and content search. Rendering stays in the palette.

import { defaultFilter } from "cmdk";
import { useContext, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  type Conversation,
  type ConversationsPage,
  type ProjectSummary,
  useProjects,
  isAbortTimeout,
  useConversations,
  useSessionSearch,
} from "@/hooks/useConversations";
import { rememberOpenSessionRow } from "@/hooks/useOpenSessionRow";
import { SidebarDataContext, useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { BOOT_PROBE_TIMEOUT_MS } from "@/lib/bootCapabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { MatchRange } from "@/components/ui/quick-pick";
import { isPullRequestLookup, matchSessions } from "./sessionMatching";
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
const EMPTY_PROJECTS: readonly ProjectSummary[] = [];
const EMPTY_PAGES: readonly ConversationsPage[] = [];

export interface PaletteSession {
  id: string;
  label: string;
  agent: string;
  archived: boolean;
  snippet: string | null;
  parent: string | null;
  project: string | null;
  /** The session's list row, which its actions read their facts from. */
  conversation: Conversation;
  /** Where a match on loaded sessions hit the label; absent for the server's matches. */
  labelRanges?: readonly MatchRange[];
}

/** Flatten list pages into palette rows, dropping repeated ids and, unless
    `includeArchived`, archived sessions. `newestFirst` re-sorts the rows by
    `updated_at`, because pushed updates reorder sessions without a refetch. */
function paletteSessions(
  pages: readonly ConversationsPage[],
  options: Parameters<typeof paletteRows>[1] = {},
): PaletteSession[] {
  return paletteRows(
    pages.flatMap((page) => page.data),
    options,
  );
}

/** `rows` with each session once, keeping its first copy. */
function uniqueRows(rows: readonly Conversation[]): Conversation[] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    if (seen.has(row.id)) return false;
    seen.add(row.id);
    return true;
  });
}

/** Palette rows for `rows`, in order unless `newestFirst`; see `paletteSessions`. */
function paletteRows(
  rows: Conversation[],
  {
    includeArchived = false,
    excludeIds = new Set(),
    newestFirst = false,
    projects = EMPTY_PROJECTS,
    parentTitles,
  }: {
    includeArchived?: boolean;
    excludeIds?: ReadonlySet<string>;
    newestFirst?: boolean;
    projects?: readonly ProjectSummary[];
    parentTitles?: ReadonlyMap<string, string | null>;
  } = {},
): PaletteSession[] {
  const seen = new Set(excludeIds);
  const out: PaletteSession[] = [];
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
      parent:
        c.family?.representative_id && c.family.representative_id !== c.id
          ? (c.family.representative_title ??
            parentTitles?.get(c.family.representative_id) ??
            "Session group")
          : null,
      project:
        projects.find((project) => project.id !== null && project.id === c.project_id)?.name ??
        c.labels?.[PROJECT_LABEL_KEY] ??
        null,
      conversation: c,
    });
  }
  return out;
}

export function usePaletteSessionSearch({
  open,
  sessionsOnly,
  matchLoaded = false,
  includeArchived,
}: {
  open: boolean;
  sessionsOnly: boolean;
  /**
   * Whether a session picker lists archived sessions, for an action that applies to them such
   * as Unarchive. Unset, ⌘K's recent list includes them and Find a session doesn't.
   */
  includeArchived?: boolean;
  /**
   * Match the loaded sessions as the user types, ahead of the server's title matches, and
   * look up a pasted `#123` or pull request link among them instead of searching.
   */
  matchLoaded?: boolean;
}) {
  const { data: projects = EMPTY_PROJECTS } = useProjects({ enabled: open });
  const sidebarRows = useContext(SidebarDataContext)?.loadedRows;
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
  // The QuickPick palette resets again in the render that opens it, so one reopened while the
  // last close is still settling never shows that query, and keys typed at once don't add to it.
  const [renderedOpen, setRenderedOpen] = useState(open);
  if (renderedOpen !== open) {
    setRenderedOpen(open);
    if (open && matchLoaded) {
      setQuery("");
      setDebouncedQuery("");
      setPageLimit(SESSION_SEARCH_PAGE_BATCH);
    }
  }

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  // ⌘K's recent list includes archived sessions, so one archived by mistake stays
  // a keystroke away; Find a session filters active ones by name.
  const recentIncludesArchived = includeArchived ?? !sessionsOnly;
  const { data, isFetching, isError, refetch, hasNextPage, fetchNextPage, isFetchNextPageError } =
    useConversations("", recentIncludesArchived, { enabled: open });
  // The sidebar's loaded sessions fill the recent rows until that list loads.
  const sidebarSessions = useOptionalLoadedConversations();
  const recentPages =
    data?.pages ?? (sessionsOnly ? undefined : sidebarSessions?.pages) ?? EMPTY_PAGES;
  const serverInfo = useServerInfo();
  // Older servers ignore `search_scope`, so they get one mixed group instead.
  const groupedSearch = serverInfo !== "loading" && serverInfo.session_search_scope === true;
  // The server's text search can't resolve a PR reference, so only loaded rows answer it.
  const pullRequestLookup = matchLoaded && isPullRequestLookup(query);
  const searching = !sessionsOnly && debouncedQuery !== "" && !pullRequestLookup;
  // Loaded sessions match at once, without waiting for the debounce or the server.
  const matchingLoaded = matchLoaded && query.trim() !== "";
  // Server matches answer the debounced query. Next to loaded matches, which follow every
  // keystroke, they show only once it has caught up, and never for a cleared query.
  const showingServerMatches = searching && (!matchLoaded || debouncedQuery === query);
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
  // A session picker's policy holds for the server's matches too; ⌘K's root search keeps archived.
  const searchIncludesArchived = includeArchived ?? true;
  const searchOptions = {
    enabled: open && searching && !awaitingServerInfo,
    limit: SEARCH_GROUP_PAGE_SIZE,
    includeArchived: searchIncludesArchived,
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

  const parentTitles = useMemo(
    () =>
      new Map(
        [recentPages, titleSearch.data?.pages ?? [], contentSearch.data?.pages ?? []]
          .flat()
          .flatMap((page) => page.data)
          .map((row) => [row.id, row.title]),
      ),
    [recentPages, titleSearch.data, contentSearch.data],
  );
  const sessions = useMemo(() => {
    const serverMatches = showingServerMatches
      ? paletteSessions(titleSearch.data?.pages ?? [], {
          includeArchived: searchIncludesArchived,
          projects,
          parentTitles,
        })
      : [];
    if (matchingLoaded) {
      // The sidebar's pinned, folder and scoped rows too, which this list's window can miss.
      // Its lists are active-only, so a row archived after it loaded is left out; the recent
      // list and the server follow the search's archived policy.
      const loaded = uniqueRows([
        ...recentPages.flatMap((page) => page.data),
        ...(sidebarRows ?? []).filter((row) => !row.archived),
      ]).filter((row) => searchIncludesArchived || !row.archived);
      // A server group's page, so a short query doesn't list every loaded session; the
      // session switcher keeps its own bound, applied below.
      const allMatches = matchSessions(loaded, query);
      const matches = sessionsOnly ? allMatches : allMatches.slice(0, SEARCH_GROUP_PAGE_SIZE);
      const rangesById = new Map(matches.map((match) => [match.session.id, match.labelRanges]));
      const local = paletteRows(
        matches.map((match) => match.session),
        { includeArchived: searchIncludesArchived, projects, parentTitles },
      ).map((session) => ({ ...session, labelRanges: rangesById.get(session.id) }));
      const listed = new Set(local.map((session) => session.id));
      return [...local, ...serverMatches.filter((session) => !listed.has(session.id))];
    }
    if (showingServerMatches) return serverMatches;
    // Archiving patches cached rows in place, so the active-only rows drop them here.
    const out = paletteSessions(recentPages, {
      includeArchived: recentIncludesArchived,
      newestFirst: true,
      projects,
      parentTitles,
    });
    if (sessionsOnly) {
      if (!query.trim()) return out;
      return out
        .map((session) => ({ ...session, score: defaultFilter(session.label, query.trim()) }))
        .filter((session) => session.score > 0)
        .sort((first, second) => second.score - first.score);
    }
    // The full first page would push Actions below the fold.
    return out.slice(0, IDLE_SESSION_LIMIT);
  }, [
    recentPages,
    recentIncludesArchived,
    sidebarRows,
    titleSearch.data,
    query,
    showingServerMatches,
    matchingLoaded,
    sessionsOnly,
    searchIncludesArchived,
    projects,
    parentTitles,
  ]);
  // Excludes title matches again in case a rename moved a session between the
  // two groups' requests: list rows need unique ids.
  const contentSessions = useMemo(
    () =>
      showingServerMatches
        ? paletteSessions(contentSearch.data?.pages ?? [], {
            includeArchived: searchIncludesArchived,
            projects,
            parentTitles,
            excludeIds: new Set(sessions.map((s) => s.id)),
          })
        : [],
    [
      contentSearch.data,
      showingServerMatches,
      searchIncludesArchived,
      sessions,
      projects,
      parentTitles,
    ],
  );
  const visibleSessions = sessionsOnly ? sessions.slice(0, SESSION_SEARCH_RESULT_LIMIT) : sessions;
  const searchGroups = [titleSearch, contentSearch];
  const loadError =
    (sessionsOnly || query === debouncedQuery) &&
    (searching ? searchGroups.some((group) => group.isError) : isError || isFetchNextPageError);
  const searchTimedOut = searchGroups.some((group) => isAbortTimeout(group.error));
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
  // Search results and ⌘K's archived-inclusive recent list are dropped once unobserved;
  // the sidebar lists the opened session from this copy, which keeps its owner.
  const rememberSearchRow = (id: string): void => {
    const paletteOwnedPages = recentIncludesArchived ? (data?.pages ?? []) : [];
    const searchRow = [titleSearch.data, contentSearch.data]
      .flatMap((results) => results?.pages ?? [])
      .concat(paletteOwnedPages)
      .flatMap((page) => page.data)
      .find((row) => row.id === id);
    if (searchRow) rememberOpenSessionRow(queryClient, searchRow);
  };

  return {
    query,
    setQuery,
    debouncedQuery,
    searching,
    /** Whether the server's matches answer the query shown, so they and their paging list. */
    showingServerMatches,
    sessions,
    visibleSessions,
    contentSessions,
    titleSearch,
    contentSearch,
    isFetching,
    loadError,
    loadMoreError,
    searchTimedOut,
    searchFetching,
    retrying,
    retryLoad,
    /** Sessions-only paging stopped at its bound with more pages on the server. */
    olderSessionsAvailable: hasNextPage === true && loadedPages >= pageLimit,
    searchOlderSessions: (): void => setPageLimit(loadedPages + SESSION_SEARCH_PAGE_BATCH),
    rememberSearchRow,
    /** The query is a `#123` or pull request link, looked up among the loaded sessions. */
    pullRequestLookup,
  };
}
