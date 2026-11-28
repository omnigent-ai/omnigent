// What shows once a pull request is linked to a session, from a chat link or the
// Link dialog: the one place that decides, so no mounted panel or tab moves on its
// own. The tabs sidebar shows the linked pull request in its own tab.

import type { QueryClient } from "@tanstack/react-query";

import type { ShowPullRequestsOptions } from "../FileViewerContext";
import { showPullRequestInPanel } from "../pullRequestPanelView";
import { openPullRequestTabFor } from "./linkRequests";

/**
 * Show `url`, just linked to `sessionId`, in the chat still showing that session.
 * Classic's panel opens on it whenever it next shows; `classic` says whether its
 * tab opens now (a chat link) or stays as it is (the Link dialog, already there).
 */
export function showLinkedPullRequest(
  queryClient: QueryClient,
  sessionId: string,
  url: string,
  classic: ShowPullRequestsOptions["classic"],
): void {
  showPullRequestInPanel(queryClient, sessionId, url);
  openPullRequestTabFor(sessionId, { kind: "pull", url }, { classic });
}
