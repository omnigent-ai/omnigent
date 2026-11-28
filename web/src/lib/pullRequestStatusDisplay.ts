import {
  type MergeReadinessStatus,
  MERGE_READINESS_TONE_TEXT,
  type MergeReadinessTone,
  presentMergeReadiness,
  type MergeReadinessView,
} from "@/lib/mergeReadiness";
import { mergeReadinessMeaning, PULL_REQUEST_STATUS_ICONS } from "@/lib/pullRequestStatusIcons";
import {
  getPullRequestStatus,
  type PullRequestStatus,
  pullRequestStatusPresentation,
  type PullRequestStatusPresentation,
  type PullRequestStatusSummary,
} from "@/lib/pullRequestStatus";

export interface PullRequestStatusDisplay extends PullRequestStatusPresentation {
  /** Stable for tests and styling: GitHub's status, or `merge:<status>` for merge readiness. */
  key: string;
  /** A shorter label for narrow places; `label` stays the accessible name. */
  short: string;
  /** The status tint, which `className` colors text with and a badge tints its fill with. */
  tone: MergeReadinessTone;
  /** Why, in one line, for tooltips; absent when the label says it. */
  detail?: string;
}

/** GitHub's statuses on merge readiness's scale of tints, with their short labels. */
const GITHUB_STATUSES: Record<
  PullRequestStatus,
  { short: string; tone: MergeReadinessTone; detail?: string }
> = {
  conflict: {
    short: "Conflicts",
    tone: "red",
    detail: "Resolve the merge conflicts with the base branch.",
  },
  failed: { short: "Checks failing", tone: "red" },
  ready: { short: "Ready", tone: "green", detail: "Checks pass and GitHub can merge it." },
  pending: { short: "Checks running", tone: "yellow", detail: "Checks are still running." },
  open: { short: "Open", tone: "green" },
  draft: { short: "Draft", tone: "gray" },
  unknown: { short: "Unknown", tone: "gray" },
  merged: { short: "Merged", tone: "purple" },
  closed: { short: "Closed", tone: "red" },
};

function failingChecksDetail(pr: PullRequestStatusSummary | null | undefined): string | undefined {
  const failing = pr?.checks?.failing ?? 0;
  if (failing === 0) return undefined;
  return failing === 1 ? "1 check is failing." : `${failing} checks are failing.`;
}

/** Sort priorities on GitHub's scale, so mixed lists order sensibly. */
const READINESS_PRIORITIES: Record<MergeReadinessStatus, number> = {
  conflicts: 0,
  checks_failing: 1,
  ci_failing: 1,
  policy_failing: 1,
  returned: 1,
  changes_requested: 1,
  ready: 2,
  approved: 2,
  needs_reviewer: 3,
  pending_approval: 3,
  pending: 3,
  ci_running: 3,
  review_required: 3,
  open: 4,
  draft: 5,
  unknown: 6,
  merged: 7,
  closed: 8,
};

/**
 * Whether merge readiness decides this PR's status: it is on for the PR, and the PR isn't
 * confirmed merged or closed, which beats a readiness read from before.
 */
export function mergeReadinessApplies(
  view: MergeReadinessView | undefined,
  state: string | null | undefined,
): view is Exclude<MergeReadinessView, { kind: "off" }> {
  if (!view || view.kind === "off") return false;
  const lifecycle = state?.toUpperCase();
  return lifecycle !== "MERGED" && lifecycle !== "CLOSED";
}

export function pullRequestStatusDisplay(
  pr: PullRequestStatusSummary | null | undefined,
  readiness?: MergeReadinessView,
): PullRequestStatusDisplay {
  if (mergeReadinessApplies(readiness, pr?.state)) {
    const presentation = presentMergeReadiness(readiness);
    if (presentation) {
      const status: MergeReadinessStatus | "checking" =
        readiness.kind === "known"
          ? readiness.readiness.status
          : readiness.kind === "checking"
            ? "checking"
            : "unknown";
      const priority = status === "checking" ? 6 : READINESS_PRIORITIES[status];
      return {
        key: `merge:${status}`,
        label: presentation.label,
        short: presentation.short,
        tone: presentation.tone,
        detail: presentation.detail,
        className: MERGE_READINESS_TONE_TEXT[presentation.tone],
        Icon: PULL_REQUEST_STATUS_ICONS[mergeReadinessMeaning(readiness)],
        priority,
      };
    }
  }
  const status = getPullRequestStatus(pr);
  const { detail, ...github } = GITHUB_STATUSES[status];
  return {
    key: status,
    ...pullRequestStatusPresentation(status),
    ...github,
    detail: status === "failed" ? failingChecksDetail(pr) : detail,
  };
}

/** Statuses the header's lifecycle pill already says, or that have nothing to say yet. */
const SAID_BY_THE_LIFECYCLE = new Set([
  "open",
  "draft",
  "merged",
  "closed",
  "unknown",
  "merge:open",
  "merge:draft",
  "merge:merged",
  "merge:closed",
  "merge:checking",
]);

/**
 * The status a PR header shows beside its lifecycle pill, or null where it would only repeat
 * the pill or is still loading. A merge status that couldn't be read shows, with its reason.
 */
export function pullRequestHeaderStatus(
  display: PullRequestStatusDisplay,
): PullRequestStatusDisplay | null {
  return SAID_BY_THE_LIFECYCLE.has(display.key) ? null : display;
}

/** Actionable PRs lead; equal statuses keep their order. */
export function sortPullRequestsByDisplay<
  T extends PullRequestStatusSummary & { mergeReadiness?: MergeReadinessView },
>(prs: readonly T[]): T[] {
  return [...prs].sort(
    (left, right) =>
      pullRequestStatusDisplay(left, left.mergeReadiness).priority -
      pullRequestStatusDisplay(right, right.mergeReadiness).priority,
  );
}
