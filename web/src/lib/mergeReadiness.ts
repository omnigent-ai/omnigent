// Whether a pull request is ready to merge, as a registered merge readiness source reads it:
// its statuses, the views a PR shows while it loads or fails, and their tints. Where no source
// is registered every PR stays `off` and keeps GitHub's own status.

import { pullRequestIntegrations } from "@/lib/pullRequestIntegrations";

export type MergeReadinessStatus =
  | "ready"
  | "approved"
  | "open"
  | "conflicts"
  | "checks_failing"
  | "ci_failing"
  | "policy_failing"
  | "returned"
  | "changes_requested"
  | "needs_reviewer"
  | "pending_approval"
  | "pending"
  | "ci_running"
  | "review_required"
  | "draft"
  | "merged"
  | "closed"
  | "unknown";

/** Display order: the order a list groups by. */
export const MERGE_READINESS_STATUSES: readonly MergeReadinessStatus[] = [
  "ready",
  "approved",
  "open",
  "conflicts",
  "checks_failing",
  "ci_failing",
  "policy_failing",
  "returned",
  "changes_requested",
  "needs_reviewer",
  "pending_approval",
  "pending",
  "ci_running",
  "review_required",
  "draft",
  "merged",
  "closed",
  "unknown",
];

/** Why a PR has its status, for the detail view's one-line explanation. */
export interface MergeReadinessReasons {
  /** Blocking CI jobs that failed, by name. */
  failingJobs: string[];
  /** Merge requirements the author must fix, by name. */
  failingRequirements: string[];
}

export interface MergeReadiness {
  status: MergeReadinessStatus;
  reasons: MergeReadinessReasons;
}

/** The statuses that mean the author can act now. */
const ATTENTION_STATUSES = new Set<MergeReadinessStatus>([
  "ready",
  "approved",
  "conflicts",
  "checks_failing",
  "ci_failing",
  "policy_failing",
  "returned",
  "changes_requested",
  "needs_reviewer",
]);

export function mergeReadinessNeedsAttention(status: MergeReadinessStatus): boolean {
  return ATTENTION_STATUSES.has(status);
}

/**
 * Why a PR's merge readiness couldn't be read for its current head: the source needs the
 * user to renew a credential, the source itself failed, or GitHub's side did.
 */
export type MergeReadinessFailure = "credential" | "service" | "github";

/**
 * What a PR shows. `off` keeps GitHub's own status; `checking` is the current head loading;
 * `unknown` is an explicit failure for the current head, never an older answer.
 */
export type MergeReadinessView =
  | { kind: "off" }
  | { kind: "checking" }
  | { kind: "known"; readiness: MergeReadiness; headSha: string }
  | { kind: "unknown"; failure: MergeReadinessFailure };

/** The known status in a view, or undefined while it's off, loading or unknown. */
export function knownMergeReadinessStatus(
  view: MergeReadinessView | undefined,
): MergeReadinessStatus | undefined {
  return view?.kind === "known" ? view.readiness.status : undefined;
}

export type MergeReadinessTone = "green" | "red" | "yellow" | "purple" | "gray";

export interface MergeReadinessPresentation {
  label: string;
  /** A shorter label for narrow columns; the full one stays the accessible name. */
  short: string;
  tone: MergeReadinessTone;
  /** Why, in one line, for the detail view and tooltips; absent when the label says it. */
  detail?: string;
}

/** Each status's label and tint; a source adds its own reasons. */
export const MERGE_READINESS_STATUS_PRESENTATIONS: Record<
  MergeReadinessStatus,
  Omit<MergeReadinessPresentation, "detail">
> = {
  ready: { label: "Ready", short: "Ready", tone: "green" },
  approved: { label: "Approved", short: "Approved", tone: "green" },
  open: { label: "Open", short: "Open", tone: "green" },
  conflicts: { label: "Conflicts", short: "Conflicts", tone: "red" },
  checks_failing: { label: "Checks failing", short: "Checks failing", tone: "red" },
  ci_failing: { label: "CI failing", short: "CI failing", tone: "red" },
  policy_failing: { label: "Policy checks failing", short: "Policy failing", tone: "red" },
  returned: { label: "Returned to you", short: "Returned", tone: "red" },
  changes_requested: { label: "Changes requested", short: "Revise", tone: "red" },
  needs_reviewer: { label: "Needs reviewer", short: "Needs reviewer", tone: "yellow" },
  pending_approval: { label: "Pending approval", short: "In review", tone: "yellow" },
  pending: { label: "Pending", short: "Pending", tone: "yellow" },
  ci_running: { label: "CI running", short: "CI running", tone: "yellow" },
  review_required: { label: "Review required", short: "Review required", tone: "yellow" },
  draft: { label: "Draft", short: "Draft", tone: "gray" },
  merged: { label: "Merged", short: "Merged", tone: "purple" },
  closed: { label: "Closed", short: "Closed", tone: "red" },
  unknown: { label: "Merge status unknown", short: "Unknown", tone: "gray" },
};

/** A status's label on its own, e.g. a Pulls list group title. */
export function mergeReadinessStatusLabel(status: MergeReadinessStatus): string {
  return MERGE_READINESS_STATUS_PRESENTATIONS[status].label;
}

/** What a source says about its statuses and failures, beyond each status's label and tint. */
export interface MergeReadinessWording {
  /** The one-line reason for a status, where its reasons don't give one. */
  details: Partial<Record<MergeReadinessStatus, string>>;
  /** How each failure to read the current head shows. */
  failures: Record<MergeReadinessFailure, MergeReadinessPresentation>;
}

/** Reasons that hold for any source; a source adds or replaces its own. */
export const MERGE_READINESS_DETAILS: Partial<Record<MergeReadinessStatus, string>> = {
  ready: "Merge requirements and blocking CI pass.",
  conflicts: "Resolve the merge conflicts with the base branch.",
  needs_reviewer: "Request a reviewer who owns the changed files.",
  pending_approval: "Waiting for a requested reviewer to approve.",
  pending: "Waiting on merge requirements.",
  ci_running: "Blocking CI jobs are still running.",
  review_required: "Waiting for a review.",
  changes_requested: "A reviewer requested changes.",
  returned: "A reviewer responded. Address the review, then ask again.",
};

function names(prefix: string, list: readonly string[]): string {
  const shown = list.slice(0, 3).join(", ");
  const more = list.length > 3 ? ` and ${list.length - 3} more` : "";
  return `${prefix}: ${shown}${more}.`;
}

/** The label, tint and reason for any view but `off`, in a source's wording. */
export function presentMergeReadinessView(
  view: Exclude<MergeReadinessView, { kind: "off" }>,
  wording: MergeReadinessWording,
): MergeReadinessPresentation {
  if (view.kind === "checking") {
    return { label: "Checking merge status…", short: "Checking…", tone: "gray" };
  }
  if (view.kind === "unknown") return wording.failures[view.failure];
  const { status, reasons } = view.readiness;
  const base = MERGE_READINESS_STATUS_PRESENTATIONS[status];
  const failingJobs =
    reasons.failingJobs.length > 0 ? names("Blocking jobs failing", reasons.failingJobs) : "";
  const failingRequirements =
    reasons.failingRequirements.length > 0
      ? names("Requirements failing", reasons.failingRequirements)
      : "";
  // Where the reasons name nothing, the source's wording, then the shared one, says why.
  const fallback = wording.details[status] ?? MERGE_READINESS_DETAILS[status];
  switch (status) {
    case "checks_failing":
      return { ...base, detail: `${failingJobs} ${failingRequirements}`.trim() || fallback };
    case "ci_failing":
      return { ...base, detail: failingJobs || fallback };
    case "policy_failing":
      return { ...base, detail: failingRequirements || fallback };
    default:
      return { ...base, detail: fallback };
  }
}

/** Whichever presentation the view has, or null where GitHub's own status shows. */
export function presentMergeReadiness(
  view: MergeReadinessView | undefined,
): MergeReadinessPresentation | null {
  const source = pullRequestIntegrations().mergeReadiness;
  if (!view || view.kind === "off" || !source) return null;
  return source.present(view);
}

export const MERGE_READINESS_TONE_TEXT: Record<MergeReadinessTone, string> = {
  green: "text-status-green",
  red: "text-status-red",
  yellow: "text-status-yellow",
  purple: "text-status-purple",
  gray: "text-muted-foreground",
};

export const MERGE_READINESS_TONE_DOT: Record<MergeReadinessTone, string> = {
  green: "bg-status-green",
  red: "bg-status-red",
  yellow: "bg-status-yellow",
  purple: "bg-status-purple",
  gray: "bg-muted-foreground/50",
};
