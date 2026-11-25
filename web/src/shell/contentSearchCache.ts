// Where a session's Search page keeps what was typed and what it found, so the page shows
// them again when it comes back, and Go to File's text search can hand both over.

import type { QueryClient } from "@tanstack/react-query";

import type { ContentSearchInput, ContentSearchResult } from "@/lib/contentSearch";

/** A search's answer with the text it answered. */
export type ContentSearchAnswer = ContentSearchResult & { query: string };

export const contentSearchInputKey = (sessionId: string) =>
  ["content-search-input", sessionId] as const;
export const contentSearchResultKey = (sessionId: string) =>
  ["content-search-result", sessionId] as const;
/** A search handed to the page while it may already show; it takes it once, then clears it. */
export const contentSearchHandOffKey = (sessionId: string) =>
  ["content-search-hand-off", sessionId] as const;

/** Gives `sessionId`'s Search page this search, and its answer when there is one. */
export function handSearchToSearchPage(
  queryClient: QueryClient,
  sessionId: string,
  input: ContentSearchInput,
  result: ContentSearchResult | undefined,
): void {
  queryClient.setQueryData(contentSearchInputKey(sessionId), { ...input });
  queryClient.setQueryData(contentSearchHandOffKey(sessionId), { ...input });
  if (result) {
    queryClient.setQueryData<ContentSearchAnswer>(contentSearchResultKey(sessionId), {
      ...result,
      query: input.query,
    });
  }
}
