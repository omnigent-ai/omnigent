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
/** A search handed to the page, which runs it itself when its answer didn't come over. */
export interface ContentSearchHandOff {
  input: ContentSearchInput;
  run: boolean;
}

/** A search handed to the page while it may already show; it takes it once, then clears it. */
export const contentSearchHandOffKey = (sessionId: string) =>
  ["content-search-hand-off", sessionId] as const;

/**
 * Gives `sessionId`'s Search page this search, and its answer when there is one. A search the
 * page still runs is cancelled first, so its late answer can't replace the handed one; with no
 * answer handed over, the page drops its older one and runs the handed search itself.
 */
export async function handSearchToSearchPage(
  queryClient: QueryClient,
  sessionId: string,
  input: ContentSearchInput,
  result: ContentSearchResult | undefined,
): Promise<void> {
  const resultKey = contentSearchResultKey(sessionId);
  await queryClient.cancelQueries({ queryKey: resultKey, exact: true });
  queryClient.setQueryData(contentSearchInputKey(sessionId), { ...input });
  if (result) {
    queryClient.setQueryData<ContentSearchAnswer>(resultKey, { ...result, query: input.query });
  } else {
    await queryClient.resetQueries({ queryKey: resultKey, exact: true });
  }
  queryClient.setQueryData<ContentSearchHandOff>(contentSearchHandOffKey(sessionId), {
    input: { ...input },
    run: !result,
  });
}
