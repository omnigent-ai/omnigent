// Request Review…, as a PR row's menus, a selection's bar and menus, and ⌘K offer it: one
// label, icon, keywords and rule for which PRs it applies to, so the surfaces can't drift.

import { UserPlusIcon } from "lucide-react";

import {
  type PullRequestCapabilities,
  pullRequestEditCapabilities,
} from "@/newPullRequestPage/hooks/usePullRequests";

export const REQUEST_REVIEW_LABEL = "Request Review…";
export const REQUEST_REVIEW_ICON = UserPlusIcon;
export const REQUEST_REVIEW_KEYWORDS = ["reviewer", "request review", "assign"] as const;

/** ⌘K's row for one PR, e.g. "Request Review on #12 Fix the build…". */
export const requestReviewPaletteLabel = (pullRequestName: string) =>
  `Request Review on ${pullRequestName}…`;

/** ⌘K's row for a selection, e.g. "Request Review on 3 Pull Requests…". */
export const requestReviewSelectionPaletteLabel = (count: number) =>
  `Request Review on ${count} Pull Request${count === 1 ? "" : "s"}…`;

/**
 * Whether reviews can be requested on a PR: `capabilities` is what its read says, null where the
 * list can't edit it.
 */
export function requestReviewOffered(
  capabilities: PullRequestCapabilities | null | undefined,
): boolean {
  return !!capabilities && pullRequestEditCapabilities({ capabilities }).reviewers;
}
