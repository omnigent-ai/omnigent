import {
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  GitCompareArrowsIcon,
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  type LucideIcon,
} from "lucide-react";

export type PullRequestStatus =
  "conflict" | "failed" | "ready" | "pending" | "open" | "draft" | "unknown" | "merged" | "closed";

/** Both stored associations and live PR details can supply a status. */
export interface PullRequestStatusSummary {
  state?: string | null;
  is_draft?: boolean;
  merge_state_status?: string | null;
  checks_supported?: boolean;
  checks?: { failing: number; pending: number; partial?: boolean };
}

export interface PullRequestStatusPresentation {
  label: string;
  className: string;
  Icon: LucideIcon;
  priority: number;
}

/** A stored summary supersedes richer details only when it reports a newer lifecycle change. */
export function hasNewerPullRequestLifecycle(
  stored: PullRequestStatusSummary & { state_checked_at?: number | null },
  live: PullRequestStatusSummary,
  liveUpdatedAt: number,
): boolean {
  if (!((stored.state_checked_at ?? 0) * 1000 > liveUpdatedAt)) return false;
  const state = stored.state?.toUpperCase();
  if (!state || !["OPEN", "MERGED", "CLOSED"].includes(state)) return false;
  return (
    state !== live.state?.toUpperCase() ||
    (state === "OPEN" && stored.is_draft !== undefined && stored.is_draft !== live.is_draft)
  );
}

const PRESENTATION: Record<PullRequestStatus, PullRequestStatusPresentation> = {
  conflict: {
    label: "Merge conflicts",
    className: "text-red-700 dark:text-red-400",
    Icon: GitCompareArrowsIcon,
    priority: 0,
  },
  failed: {
    label: "Checks failing",
    className: "text-red-700 dark:text-red-400",
    Icon: CircleXIcon,
    priority: 1,
  },
  ready: {
    label: "Ready to merge",
    className: "text-green-700 dark:text-green-400",
    Icon: CircleCheckIcon,
    priority: 2,
  },
  pending: {
    label: "Checks running",
    className: "text-amber-700 dark:text-amber-400",
    Icon: CircleDotIcon,
    priority: 3,
  },
  open: {
    label: "Open",
    className: "text-green-700 dark:text-green-400",
    Icon: GitPullRequestIcon,
    priority: 4,
  },
  draft: {
    label: "Draft",
    className: "text-muted-foreground",
    Icon: GitPullRequestDraftIcon,
    priority: 5,
  },
  unknown: {
    label: "Status unavailable",
    className: "text-muted-foreground",
    Icon: GitPullRequestIcon,
    priority: 6,
  },
  merged: {
    label: "Merged",
    className: "text-purple-700 dark:text-purple-400",
    Icon: GitMergeIcon,
    priority: 7,
  },
  closed: {
    label: "Closed",
    className: "text-red-700 dark:text-red-400",
    Icon: GitPullRequestClosedIcon,
    priority: 8,
  },
};

export function pullRequestStatusPresentation(
  status: PullRequestStatus,
): PullRequestStatusPresentation {
  return PRESENTATION[status];
}

export function getPullRequestStatus(
  pr: PullRequestStatusSummary | null | undefined,
): PullRequestStatus {
  const state = pr?.state?.toUpperCase();
  if (state === "MERGED") return "merged";
  if (state === "CLOSED") return "closed";
  if (state !== "OPEN" || !pr) return "unknown";

  const mergeState = pr.merge_state_status?.toUpperCase();
  if (mergeState === "DIRTY") return "conflict";
  if (pr.checks && pr.checks.failing > 0) return "failed";
  // Drafts still surface repairs, but running or passing checks do not make them ready.
  if (pr.is_draft) return "draft";
  if (pr.checks && pr.checks.pending > 0) return "pending";
  // Passing checks alone do not establish that required reviews or merge rules passed.
  if (mergeState === "CLEAN" && !pr.checks?.partial && pr.checks_supported !== false)
    return "ready";
  return "open";
}

/** Actionable PRs lead; equal statuses keep the server's display order. */
export function sortPullRequestsByStatus<T extends PullRequestStatusSummary>(
  prs: readonly T[],
): T[] {
  return [...prs].sort(
    (left, right) =>
      PRESENTATION[getPullRequestStatus(left)].priority -
      PRESENTATION[getPullRequestStatus(right)].priority,
  );
}
