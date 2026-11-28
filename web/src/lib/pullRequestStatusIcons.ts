// One icon per meaning for a PR's status, wherever it shows: the Pull Requests list, the sidebar,
// the composer and the PR header. Review outcomes are people and documents, never the CI
// column's circles; check results keep those circles, since they say the same thing.

import {
  CheckCheckIcon,
  CircleDotIcon,
  CircleQuestionMarkIcon,
  CircleXIcon,
  ClockIcon,
  EyeIcon,
  FileDiffIcon,
  GitCompareArrowsIcon,
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  HourglassIcon,
  KeyRoundIcon,
  Link2OffIcon,
  LoaderCircleIcon,
  type LucideIcon,
  MoonIcon,
  ServerOffIcon,
  ShieldXIcon,
  TriangleAlertIcon,
  Undo2Icon,
  UserRoundCheckIcon,
  UserRoundPlusIcon,
} from "lucide-react";
import type { MergeReadinessStatus, MergeReadinessView } from "@/lib/mergeReadiness";

/** What a status says, whichever source said it: GitHub, merge readiness, or the list itself. */
export type PullRequestStatusMeaning =
  | "review-requested"
  | "needs-reviewer"
  | "approved"
  | "changes-requested"
  | "returned"
  | "ready"
  | "merge-requested"
  | "conflicts"
  | "checks-failing"
  | "checks-running"
  | "policy-failing"
  | "checking"
  | "readiness-unknown"
  | "credential"
  | "refresh-failed"
  | "not-linked"
  | "waiting"
  | "offline"
  | "asleep"
  | PullRequestLifecycleMeaning;

/** The meanings a PR's state glyph already shows on its own. */
export type PullRequestLifecycleMeaning = "open" | "draft" | "merged" | "closed" | "unknown";

export const PULL_REQUEST_STATUS_ICONS: Record<PullRequestStatusMeaning, LucideIcon> = {
  "review-requested": EyeIcon,
  "needs-reviewer": UserRoundPlusIcon,
  approved: UserRoundCheckIcon,
  "changes-requested": FileDiffIcon,
  returned: Undo2Icon,
  ready: CheckCheckIcon,
  "merge-requested": ClockIcon,
  conflicts: GitCompareArrowsIcon,
  "checks-failing": CircleXIcon,
  "checks-running": CircleDotIcon,
  "policy-failing": ShieldXIcon,
  checking: LoaderCircleIcon,
  "readiness-unknown": CircleQuestionMarkIcon,
  credential: KeyRoundIcon,
  "refresh-failed": TriangleAlertIcon,
  "not-linked": Link2OffIcon,
  waiting: HourglassIcon,
  offline: ServerOffIcon,
  asleep: MoonIcon,
  open: GitPullRequestIcon,
  draft: GitPullRequestDraftIcon,
  merged: GitMergeIcon,
  closed: GitPullRequestClosedIcon,
  unknown: GitPullRequestIcon,
};

const READINESS_MEANINGS: Record<MergeReadinessStatus, PullRequestStatusMeaning> = {
  ready: "ready",
  approved: "approved",
  open: "open",
  conflicts: "conflicts",
  checks_failing: "checks-failing",
  ci_failing: "checks-failing",
  policy_failing: "policy-failing",
  returned: "returned",
  changes_requested: "changes-requested",
  needs_reviewer: "needs-reviewer",
  pending_approval: "review-requested",
  pending: "checks-running",
  ci_running: "checks-running",
  review_required: "review-requested",
  draft: "draft",
  merged: "merged",
  closed: "closed",
  unknown: "readiness-unknown",
};

/** What a merge readiness view says, including while it's checked and when it couldn't be read. */
export function mergeReadinessMeaning(
  view: Exclude<MergeReadinessView, { kind: "off" }>,
): PullRequestStatusMeaning {
  if (view.kind === "known") return READINESS_MEANINGS[view.readiness.status];
  if (view.kind === "checking") return "checking";
  return view.failure === "credential" ? "credential" : "readiness-unknown";
}
