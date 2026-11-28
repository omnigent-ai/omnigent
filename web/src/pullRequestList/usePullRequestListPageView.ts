// How the new Pull Requests list is shown for one session: its filter, status chip, Display
// choices and collapsed groups. Held in memory for the app's run, as the panel's own view is,
// so it survives switching sessions and tabs but isn't stored on this device.

import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { PullRequestListFilter } from "@/shell/pullRequestListModel";
import { DEFAULT_PAGE_DISPLAY, type PageDisplay } from "./pullRequestListPageModel";
import type { ListFilter } from "./pullRequestListFilterModel";

export interface PullRequestListPageView {
  query: string;
  filter: PullRequestListFilter;
  display: PageDisplay;
  /**
   * The user picked `display`, though it may equal the default; a reset clears it. Absent from
   * views kept before it was recorded.
   */
  displayChosen?: boolean;
  /** Group ids the user collapsed. */
  collapsed: readonly string[];
  /** The filter chips narrowing the list, for this run; see `pullRequestListFilterModel`. */
  filters?: readonly ListFilter[];
}

export const DEFAULT_PULL_REQUEST_LIST_PAGE_VIEW: PullRequestListPageView = {
  query: "",
  filter: "all",
  display: DEFAULT_PAGE_DISPLAY,
  collapsed: [],
  filters: [],
};

const VIEW_GC_MS = 30 * 60_000;

export function pullRequestListPageViewKey(conversationId: string) {
  return ["pull-request-list-page-view", conversationId] as const;
}

/**
 * The view as React state, so the filter field updates as it's typed in, written through
 * to the query cache's in-memory entry so a remounted list picks up where it was.
 */
export function usePullRequestListPageView(conversationId: string): {
  view: PullRequestListPageView;
  updateView: (update: (current: PullRequestListPageView) => PullRequestListPageView) => void;
} {
  const queryClient = useQueryClient();
  const remembered = (id: string) =>
    queryClient.getQueryData<PullRequestListPageView>(pullRequestListPageViewKey(id)) ??
    DEFAULT_PULL_REQUEST_LIST_PAGE_VIEW;
  const [state, setState] = useState(() => ({ conversationId, view: remembered(conversationId) }));
  // Another session's list starts from that session's own view.
  if (state.conversationId !== conversationId) {
    setState({ conversationId, view: remembered(conversationId) });
  }
  const updateView = useCallback(
    (update: (current: PullRequestListPageView) => PullRequestListPageView) => {
      // Kept as long as the panel's own view is, though nothing observes the entry.
      queryClient.setQueryDefaults(["pull-request-list-page-view"], { gcTime: VIEW_GC_MS });
      setState((current) => {
        const view = update(current.view);
        queryClient.setQueryData(pullRequestListPageViewKey(current.conversationId), view);
        return { conversationId: current.conversationId, view };
      });
    },
    [queryClient],
  );
  return { view: state.view, updateView };
}
