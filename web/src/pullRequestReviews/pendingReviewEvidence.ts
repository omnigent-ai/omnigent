// What settles a pending review change GitHub didn't confirm. Each rule needs positive
// evidence from a read made after it, as the same account: a read that's empty, failed or
// incomplete is never taken as an answer, so the change stays held until one shows it.

import type { PendingDraftBaseline, PendingReviewStart } from "@/lib/nativeBridge";

import {
  type DraftPlace,
  listedDrafts,
  type ListedDraft,
  type PendingReviewRead,
  type ReviewNodeState,
} from "./pendingReview";
import { normalizedBody } from "./reviewSubmit";

/** A held change either settled one way, or still can't be told. */
export type HoldVerdict<Settled extends string> =
  { kind: Settled; commentId?: string; reviewId?: string } | { kind: "held" };

const HELD = { kind: "held" } as const;

/**
 * Whether `draft` is in the scope a draft was sent to: a reply to the thread's root comment, or
 * a new conversation on exactly the sent lines, on the same side of the diff. A draft whose side
 * the read can't say, as one no thread placed, never matches lines.
 */
function inScope(
  draft: ListedDraft,
  scope: PendingDraftBaseline["scope"],
  place: DraftPlace,
): boolean {
  if (scope.kind === "reply") {
    return (
      place.kind === "reply" &&
      place.threadId === scope.threadId &&
      scope.rootCommentId !== null &&
      draft.reply_to_id === scope.rootCommentId
    );
  }
  if (place.kind !== "lines") return false;
  // The shell's baseline and the anchor sent must name the same lines.
  if (
    scope.path !== place.path ||
    scope.line !== place.line ||
    scope.startLine !== place.startLine
  ) {
    return false;
  }
  // A range starts on the side it ends on, as it's sent; a single line has no start side.
  const startSide = place.startLine === null ? null : place.side;
  return (
    draft.reply_to_id === null &&
    draft.path === place.path &&
    draft.line === place.line &&
    draft.start_line === place.startLine &&
    draft.side === place.side &&
    (startSide === null || draft.start_side === startSide)
  );
}

/**
 * The draft an unconfirmed add made: exactly one draft in its review and scope that the baseline
 * didn't have, with the body sent. None, or more than one, isn't an answer; nor is a baseline
 * or a read that couldn't list everything.
 */
export function draftAddVerdict(
  sent: { baseline: PendingDraftBaseline | null; body: string; place: DraftPlace },
  read: PendingReviewRead,
): HoldVerdict<"landed"> {
  const { baseline, place } = sent;
  if (!baseline?.complete || !read.complete || read.review?.id !== baseline.reviewId) return HELD;
  const known = new Set(baseline.knownCommentIds);
  const body = normalizedBody(sent.body);
  const matches = listedDrafts(read).filter(
    (draft) =>
      !known.has(draft.id) &&
      inScope(draft, baseline.scope, place) &&
      normalizedBody(draft.body) === body,
  );
  return matches.length === 1 ? { kind: "landed", commentId: matches[0].id } : HELD;
}

/**
 * The review an unconfirmed start made: with none pending before it, the account's pending
 * review on the commit it was pinned to. Anything else, or no review, stays held.
 */
export function reviewStartVerdict(
  start: PendingReviewStart | null,
  read: PendingReviewRead,
): HoldVerdict<"started"> {
  if (!start || start.priorPendingReviewId !== null) return HELD;
  const review = read.review;
  return review && review.commit_oid === start.commitOid
    ? { kind: "started", reviewId: review.id }
    : HELD;
}

/** An unconfirmed edit landed once the draft, still in its review, reads with the new body. */
export function draftEditVerdict(
  sent: { commentId: string; reviewId: string; body: string },
  read: PendingReviewRead,
): HoldVerdict<"landed"> {
  if (read.review?.id !== sent.reviewId) return HELD;
  const draft = listedDrafts(read).find((listed) => listed.id === sent.commentId);
  return draft && normalizedBody(draft.body) === normalizedBody(sent.body)
    ? { kind: "landed", commentId: sent.commentId }
    : HELD;
}

/**
 * An unconfirmed delete landed once a complete read of the same pending review lacks the draft.
 * The review gone or replaced says nothing: it may have been submitted with the draft in it.
 */
export function draftDeleteVerdict(
  sent: { commentId: string; reviewId: string },
  read: PendingReviewRead,
): HoldVerdict<"landed"> {
  if (!read.complete || read.review?.id !== sent.reviewId) return HELD;
  return listedDrafts(read).some((draft) => draft.id === sent.commentId)
    ? HELD
    : { kind: "landed", commentId: sent.commentId };
}

/**
 * Where an unconfirmed discard stands, from the review read by its id as the same account, with
 * the pull request still readable: gone only when GitHub says it is, submitted elsewhere when it
 * reads submitted, and still pending when it is. A read that can't say keeps it held.
 */
export function discardVerdict(
  reviewId: string,
  state: ReviewNodeState,
): HoldVerdict<"discarded" | "not_discarded" | "submitted_elsewhere"> {
  switch (state) {
    case "absent":
      return { kind: "discarded", reviewId };
    case "PENDING":
      return { kind: "not_discarded", reviewId };
    case "unreadable":
      return HELD;
    default:
      return { kind: "submitted_elsewhere", reviewId };
  }
}
