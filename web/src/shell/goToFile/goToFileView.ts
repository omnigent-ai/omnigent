// What Go to File lists for the text typed now: the session's recent files before a query,
// then the workspace's ranked matches, with the line that says what the search covered.
// Pure, so every state of the dialog is a plain input.

import type { QuickPickItem } from "@/components/ui/quick-pick";

import { rankGoToFileResults, stillMatches } from "../goToFileRanking";
import {
  GO_TO_FILE_VISIBLE_LIMIT,
  type GoToFileSearchState,
  goToFileStatusMessage,
} from "../goToFileSearchStatus";
import { type GoToFileTarget, rankedFileItems, recentFileItems } from "./goToFileItems";

export interface GoToFileView {
  items: QuickPickItem<GoToFileTarget>[];
  /** Shown in place of the list when it's empty. */
  emptyMessage: string;
  /** The line under a non-empty list, or null when the rows speak for themselves. */
  status: string | null;
}

/** Before a query, with no recent files: what to do. */
export const NO_RECENT_FILES = "Type part of a file name or path.";

export function goToFileView(
  state: GoToFileSearchState,
  query: string,
  recentPaths: readonly string[],
): GoToFileView {
  if (state.phase === "idle") {
    return { items: recentFileItems(recentPaths), emptyMessage: NO_RECENT_FILES, status: null };
  }
  // While a newer query runs, an earlier answer's rows show only where they still match.
  const files = (state.response?.files ?? []).filter(
    (file) => state.phase !== "searching" || stillMatches(file.path, query),
  );
  const ranked = rankGoToFileResults(files, query);
  const message = goToFileStatusMessage(state, query, ranked.length);
  const items = rankedFileItems(ranked.slice(0, GO_TO_FILE_VISIBLE_LIMIT));
  return {
    items,
    emptyMessage: message ?? NO_RECENT_FILES,
    status: items.length > 0 ? message : null,
  };
}
