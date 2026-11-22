// What Go to File says about its search. The server returns at most 500 substring
// matches and may stop early, so the dialog must never present a partial,
// stale or failed answer as the complete list of files.

import type { WorkspaceFileSearchResult } from "@/hooks/useWorkspaceChangedFiles";

/** The server's per-request result cap; a response this full may be incomplete. */
export const GO_TO_FILE_RESULT_LIMIT = 500;
/** Rows drawn at once; more slow the keyboard down without helping anyone pick. */
export const GO_TO_FILE_VISIBLE_LIMIT = 100;

/** The search as the dialog sees it: the text typed now, and the query it last ran. */
export interface GoToFileSearchSnapshot {
  /** The text in the input right now. */
  query: string;
  /** The debounced text the search ran for. */
  searchedQuery: string;
  /** False when the session's workspace is known to be unreachable. */
  serveable: boolean | undefined;
  data: WorkspaceFileSearchResult | undefined;
  isSuccess: boolean;
  isError: boolean;
  isFetching: boolean;
  isPlaceholderData: boolean;
}

export type GoToFileSearchPhase =
  /** Nothing typed yet. */
  | "idle"
  /** The session's workspace can't be reached or searched. */
  | "unavailable"
  /** The latest search failed. */
  | "error"
  /** Waiting for the debounce or a response; earlier rows may still show. */
  | "searching"
  /** The response for what's typed has arrived. */
  | "settled";

export interface GoToFileSearchState {
  phase: GoToFileSearchPhase;
  /**
   * The response to rank, if any. While searching it's an earlier query's, so
   * only rows that still match the typed text should be shown.
   */
  response: WorkspaceFileSearchResult | undefined;
}

/** Where the search stands, from the hook's state for the text typed now. */
export function goToFileSearchState(snapshot: GoToFileSearchSnapshot): GoToFileSearchState {
  const typed = snapshot.query.trim();
  if (!typed) return { phase: "idle", response: undefined };
  if (snapshot.serveable === false) return { phase: "unavailable", response: undefined };
  const debouncing = typed !== snapshot.searchedQuery.trim();
  if (!debouncing && snapshot.isError && !snapshot.isFetching) {
    return { phase: "error", response: undefined };
  }
  const settled =
    !debouncing && snapshot.isSuccess && !snapshot.isPlaceholderData && !snapshot.isFetching;
  if (!settled) return { phase: "searching", response: snapshot.data };
  // The workspace answered that it can't be searched: not "nothing matched".
  if (snapshot.data?.unavailable) return { phase: "unavailable", response: undefined };
  return { phase: "settled", response: snapshot.data };
}

/**
 * The status line under the results, or null when the rows speak for themselves.
 *
 * @param matchCount Files that matched after ranking, before the visible limit.
 */
export function goToFileStatusMessage(
  state: GoToFileSearchState,
  query: string,
  matchCount: number,
): string | null {
  switch (state.phase) {
    case "idle":
      return "Type part of a file name or path.";
    case "unavailable":
      return "This session's files aren't available right now.";
    case "error":
      return "Couldn't search this workspace. Check that the session is connected and try again.";
    case "searching":
      return "Searching…";
    case "settled":
      return settledMessage(state.response, query.trim(), matchCount);
  }
}

function settledMessage(
  response: WorkspaceFileSearchResult | undefined,
  typed: string,
  matchCount: number,
): string | null {
  const truncated = !!response?.truncated;
  const capped =
    !!response && (response.hasMore || response.files.length >= GO_TO_FILE_RESULT_LIMIT);
  if (matchCount === 0) {
    if (truncated) return "No matches found before the search stopped. Keep typing to narrow it.";
    if (capped) return "No files found among the first matches. Keep typing to narrow the search.";
    return `No files match “${typed}”.`;
  }
  if (truncated) {
    return "The search stopped before covering the whole workspace. Keep typing to narrow it.";
  }
  if (matchCount > GO_TO_FILE_VISIBLE_LIMIT) {
    return `Showing the top ${GO_TO_FILE_VISIBLE_LIMIT} matches. Keep typing to narrow the search.`;
  }
  if (capped) return "Showing the first matches. Keep typing to narrow the search.";
  return null;
}
