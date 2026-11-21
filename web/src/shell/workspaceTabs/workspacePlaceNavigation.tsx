// How the workspace sidebar's places ask for other places: a file shown in its diff or its
// own tab, and a session's pull requests shown in their tabs. The sidebar provides it.

import { createContext, useContext } from "react";

import type { OpenFileOptions } from "@/shell/FileViewerContext";

import type { PullRequestView } from "./workspacePlace";

/** A file open from the sidebar: a cited position, and how to show the file. */
export type WorkspaceFileOpenOptions = OpenFileOptions & {
  /** Show the file in the Changes review instead. */
  view?: "diff";
  /** Open it in a tab of its own, such as from a ⌥-click in the tree. */
  newTab?: boolean;
};

/** What to show of a session's pull requests: all of them, or one pull request. */
export type PullRequestsTarget =
  | { kind: "overview" }
  /**
   * One PR. With `overview`, a tab already showing it returns to its Overview too. A new tab opens on
   * `view` unless the URL names a section; one already showing the PR keeps its own.
   */
  | { kind: "pull"; url: string; overview?: true; view?: PullRequestView };

export interface ShowPullRequestsOptions {
  /** Open or keep the tab behind the current one, as a browser's ⌘-click does. */
  background?: boolean;
}

export type ShowPullRequests = (
  target: PullRequestsTarget,
  options?: ShowPullRequestsOptions,
) => void;

const ShowPullRequestsContext = createContext<ShowPullRequests | null>(null);

/** Gives the places inside it a way to show pull requests in the sidebar's tabs. */
export const ShowPullRequestsProvider = ShowPullRequestsContext.Provider;

/** Shows a session's pull requests in the sidebar's tabs; null outside the sidebar. */
export function useShowPullRequests(): ShowPullRequests | null {
  return useContext(ShowPullRequestsContext);
}
