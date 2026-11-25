// What the Pull Requests panel shows for a session: the selected PR, whether its
// details are open, and the tab. Kept in the query cache, so it survives the
// panel unmounting and another surface can choose what it opens on.

import type { QueryClient } from "@tanstack/react-query";

export type PullRequestSubtab = "summary" | "comments" | "commits" | "changes";

export interface PullRequestPanelView {
  selected?: string;
  showingDetails: boolean;
  subtab: PullRequestSubtab;
  /** The commit open in the Commits tab, if any. */
  commit?: string;
  /** A file in the Changes tab, when a tab's place names one. */
  file?: { path: string };
}

/**
 * A pull request chosen from a list, on `subtab`, the section the reader was on, as moving
 * through a stack keeps it. A commit or file of the pull request it leaves stays behind.
 */
export function viewForChosenPullRequest(
  url: string,
  subtab: PullRequestSubtab,
): PullRequestPanelView {
  return { selected: url, showingDetails: true, subtab, commit: undefined, file: undefined };
}

/** A part of a pull request that can open in a tab of its own: one commit, or one changed file. */
export type PullRequestNewTabTarget = { commit: string } | { path: string };

export const DEFAULT_PANEL_VIEW: PullRequestPanelView = {
  showingDetails: false,
  subtab: "summary",
};

export const pullRequestPanelViewKey = (sessionId: string) =>
  ["pull-request-panel-view", sessionId] as const;

/** Make the session's panel show its list of pull requests, now if it's open or once it opens. */
export function showPullRequestListInPanel(queryClient: QueryClient, sessionId: string): void {
  queryClient.setQueryData<PullRequestPanelView>(pullRequestPanelViewKey(sessionId), (current) => ({
    ...(current ?? DEFAULT_PANEL_VIEW),
    showingDetails: false,
    subtab: "summary",
    commit: undefined,
    file: undefined,
  }));
}

/** Make the session's panel show `url`'s details, now if it's open or once it opens. */
export function showPullRequestInPanel(
  queryClient: QueryClient,
  sessionId: string,
  url: string,
): void {
  queryClient.setQueryData<PullRequestPanelView>(pullRequestPanelViewKey(sessionId), {
    selected: url,
    showingDetails: true,
    subtab: "summary",
  });
}
