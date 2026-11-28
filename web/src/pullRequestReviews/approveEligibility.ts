// When an approval may be sent, one rule for every place that sends one: a row's one-click
// Approve, the Pull Requests page's bulk Approve, and the Review menu's. Whether a PR asks
// anyone for a review never matters here; where a row shows Approve is its own, stricter rule.

import { verdictUnavailableReason, type ReviewSubmitEntry } from "./reviewSubmit";

/**
 * How the approval is sent: with no comment, as a row or a batch sends it, so a draft held in
 * the Review menu stops it; or with the menu's own draft as its comment.
 */
export type ApproveMode = "no-comment" | "with-body";

export interface ApproveFacts {
  mode: ApproveMode;
  /** Whether the PR is open now; null where that isn't known. */
  open: boolean | null;
  author: string | null | undefined;
  /** The login it would be sent as; null where this Mac can't send it. */
  actor: string | null;
  /** This Mac submits reviews on the PR as `actor`. */
  reviewsOffered: boolean;
  /** The commit it would approve; null until a read names it. */
  headSha: string | null | undefined;
  /** The Review menu's entry for the PR and `actor`. */
  entry: Pick<ReviewSubmitEntry, "status" | "body"> | null;
}

export const APPROVE_STATE_UNKNOWN = "Its state isn’t known yet.";
export const APPROVE_HELD = "A review of it is still being written or sent.";

/** Whether `entry` holds a review that a new approval in `mode` must wait for. */
export function approvalHeld(
  entry: Pick<ReviewSubmitEntry, "status" | "body"> | null,
  mode: ApproveMode,
): boolean {
  if (!entry) return false;
  const { kind } = entry.status;
  if (kind === "uncertain" || kind === "submitting") return true;
  return mode === "no-comment" && !!entry.body.trim();
}

/** Why the approval can't be sent now, or null where it can. */
export function approveUnavailableReason(facts: ApproveFacts): string | null {
  if (facts.open === null) return APPROVE_STATE_UNKNOWN;
  if (!facts.open) return "It’s no longer open.";
  if (!facts.actor || !facts.reviewsOffered) return "Reviews can’t be sent from this Mac.";
  const own = verdictUnavailableReason("APPROVE", facts.actor, facts.author);
  if (own) return own;
  if (approvalHeld(facts.entry, facts.mode)) return APPROVE_HELD;
  if (!facts.headSha) return "Its latest commit isn’t known yet.";
  return null;
}
