// The finder's Sessions tab: ⌘K's own session search over the sessions the user can see,
// with the typed query. Before a query it lists recent sessions; after one, the sessions whose
// names match, then those whose messages do, each with the server's paging.

import { useEffect, useMemo } from "react";

import type { QuickPickItem } from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";

import {
  type MoreRowTarget,
  paletteSessionItem,
  paletteShowMoreItems,
  type SessionRowTarget,
} from "../paletteSessionItems";
import { usePaletteSessionSearch } from "../usePaletteSessionSearch";

export const SESSION_SEARCH_COPY = {
  tab: "Sessions",
  title: "Search in Sessions",
  label: "Search sessions",
  placeholder: "Search session names and messages…",
  idle: "Type to search your sessions' names and messages.",
  empty: "No sessions match. Try other words from a message or the session's name.",
  searching: "Searching sessions…",
  failed: "Couldn't search sessions. Check your connection and try again.",
} as const;

export const RECENT_SESSIONS_GROUP = "Recent sessions";
export const NAME_MATCHES_GROUP = "Sessions";
export const MESSAGE_MATCHES_GROUP = "In messages";

export type SessionsFinderTarget = SessionRowTarget | MoreRowTarget;

export interface SessionsFinderView {
  items: QuickPickItem<SessionsFinderTarget>[];
  emptyMessage: string;
  /** The listed session behind a row, for its preview; undefined for a paging row. */
  sessionFor: (id: string) => Conversation | undefined;
  /** Keeps a found session's row so the sidebar can list it once it opens. */
  rememberRow: (id: string) => void;
}

/** Searches only while `active`, so the Files and Text tabs never ask for sessions. */
export function useSessionsFinderView(active: boolean, query: string): SessionsFinderView {
  const search = usePaletteSessionSearch({
    open: active,
    sessionsOnly: false,
    matchLoaded: true,
    includeArchived: false,
  });
  const { setQuery } = search;
  // The finder owns the query, so all three tabs search the same text.
  useEffect(() => {
    if (active) setQuery(query);
  }, [active, query, setQuery]);

  const {
    query: searchedQuery,
    debouncedQuery,
    visibleSessions,
    contentSessions,
    showingServerMatches,
    titleSearch,
    contentSearch,
    searchFetching,
    loadError,
  } = search;
  const typed = searchedQuery.trim() !== "";
  const items = useMemo(() => {
    const highlight = { labelQuery: searchedQuery, snippetQuery: debouncedQuery };
    if (!typed) {
      return visibleSessions.map((session) =>
        paletteSessionItem(session, RECENT_SESSIONS_GROUP, highlight),
      );
    }
    return [
      ...visibleSessions.map((session) =>
        paletteSessionItem(session, NAME_MATCHES_GROUP, highlight),
      ),
      ...(showingServerMatches
        ? paletteShowMoreItems("title", titleSearch, visibleSessions, NAME_MATCHES_GROUP)
        : []),
      ...contentSessions.map((session) =>
        paletteSessionItem(session, MESSAGE_MATCHES_GROUP, highlight),
      ),
      ...paletteShowMoreItems("content", contentSearch, contentSessions, MESSAGE_MATCHES_GROUP),
    ];
  }, [
    typed,
    searchedQuery,
    debouncedQuery,
    visibleSessions,
    contentSessions,
    showingServerMatches,
    titleSearch,
    contentSearch,
  ]);

  let emptyMessage: string = SESSION_SEARCH_COPY.idle;
  if (typed) {
    if (loadError) emptyMessage = SESSION_SEARCH_COPY.failed;
    else if (searchFetching || searchedQuery !== debouncedQuery) {
      emptyMessage = SESSION_SEARCH_COPY.searching;
    } else emptyMessage = SESSION_SEARCH_COPY.empty;
  }

  return {
    items,
    emptyMessage,
    sessionFor: (id) =>
      [...visibleSessions, ...contentSessions].find((session) => session.id === id)?.conversation,
    rememberRow: search.rememberSearchRow,
  };
}
