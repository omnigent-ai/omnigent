// The pull request page's sections, in the order its tabs, picker and measurements list them.

import type { PullRequestChangedFilesResult } from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

import type { PullRequestDetailsState } from "./detailsLayout";

export interface PullRequestPageSection {
  id: PullRequestSubtab;
  label: string;
  /** The tab's count; absent while unknown. */
  count?: number;
  /** More exist beyond `count`. */
  partial?: boolean;
}

/** Overview, Commits where the PR shows them, and Changes; Commits and Changes count once known. */
export function pullRequestPageSections(
  state: Pick<PullRequestDetailsState, "tabs">,
): PullRequestPageSection[] {
  const { tabs } = state;
  return [
    { id: "summary", label: "Overview" },
    ...(tabs.commits.shown
      ? [{ id: "commits" as const, label: "Commits", count: tabs.commits.count }]
      : []),
    { id: "changes", label: "Changes", ...tabs.changes },
  ];
}

/**
 * The Changes tab's count from the changed-files read: GitHub's total where it gives one, else
 * the files listed, a lower bound when the list was cut short. Unknown until the read answers.
 */
export function changedFilesTabCount(
  data:
    | Pick<PullRequestChangedFilesResult, "available" | "data" | "has_more" | "total_count">
    | undefined,
): { count?: number; partial?: boolean } {
  if (!data || data.available === false) return {};
  if (typeof data.total_count === "number") return { count: data.total_count };
  return { count: data.data.length, ...(data.has_more ? { partial: true } : {}) };
}
