// PR display helpers shared by the PR panel's strip and the sidebar session tooltip.

import {
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  type LucideIcon,
} from "lucide-react";
import type { PullRequest } from "@/hooks/usePullRequests";

/** GitHub's glyph, label and tint for a PR state; gh reports a draft as OPEN + is_draft. */
export function pullRequestStatus(pr: Pick<PullRequest, "state" | "is_draft">): {
  icon: LucideIcon;
  label: string;
  className: string;
} {
  const state = pr.state.toUpperCase();
  if (state === "MERGED") {
    return {
      icon: GitMergeIcon,
      label: "Merged",
      className: "text-purple-600 dark:text-purple-400",
    };
  }
  if (state === "CLOSED") {
    return {
      icon: GitPullRequestClosedIcon,
      label: "Closed",
      className: "text-red-600 dark:text-red-400",
    };
  }
  if (pr.is_draft) {
    return { icon: GitPullRequestDraftIcon, label: "Draft", className: "text-muted-foreground" };
  }
  return {
    icon: GitPullRequestIcon,
    label: "Open",
    className: "text-green-600 dark:text-green-400",
  };
}
