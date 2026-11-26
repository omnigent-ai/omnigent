// Runs Go to File's text search as the query and options settle, and reports its state for
// `textSearchView`. Each answer is cached per search, so going back to an earlier one is instant.

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  contentSearchProblem,
  type ContentSearchInput,
  type ContentSearchResult,
  runContentSearch,
} from "@/lib/contentSearch";

import type { TextSearchState } from "./textSearchView";

/** Waits out fast typing; each search scans the workspace's files. */
export const TEXT_SEARCH_DEBOUNCE_MS = 200;

export interface GoToFileTextSearch {
  state: TextSearchState;
  /** The last answer, for "Open in Search" to show without searching again. */
  result: ContentSearchResult | undefined;
}

/** An answer with the search that produced it, so its lines are marked for that search. */
interface SearchedAnswer {
  result: ContentSearchResult;
  input: ContentSearchInput;
}

export function useGoToFileTextSearch(
  sessionId: string,
  input: ContentSearchInput,
  enabled: boolean,
): GoToFileTextSearch {
  const searched = useDebouncedValue(input, TEXT_SEARCH_DEBOUNCE_MS);
  const problem = contentSearchProblem(input);
  const runnable =
    enabled && searched.query.trim() !== "" && contentSearchProblem(searched) === null;
  const search = useQuery({
    queryKey: ["go-to-file-text-search", sessionId, searched],
    queryFn: async ({ signal }): Promise<SearchedAnswer> => ({
      result: await runContentSearch(sessionId, searched, signal),
      input: searched,
    }),
    enabled: runnable,
    retry: false,
    staleTime: 30_000,
    gcTime: 60_000,
    placeholderData: keepPreviousData,
  });
  if (input.query.trim() === "") return { state: { phase: "idle" }, result: undefined };
  if (problem) return { state: { phase: "invalid", problem }, result: undefined };
  if (searched !== input || search.isFetching || search.isPlaceholderData || search.isPending) {
    return { state: { phase: "searching", previous: search.data }, result: undefined };
  }
  if (search.isError) {
    return { state: { phase: "error", message: search.error.message }, result: undefined };
  }
  return { state: { phase: "settled", result: search.data.result }, result: search.data.result };
}
