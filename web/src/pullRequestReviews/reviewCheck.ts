// Check Again for a review GitHub may have made: a read of the viewer's reviews as the login it
// was sent as, in its scope, under that login's sign-in now. Only the review it was asked for
// settles; the PR's own Review menu and the Pull Requests page's list both run it.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import type { QueryClient } from "@tanstack/react-query";

import { showToast } from "@/components/ui/toast";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  draftOwner,
  macRouteAs,
  macRouteCurrent,
  type MacWriteRoute,
  parseReviewCheckAnswer,
  type ReviewCheck,
} from "@/lib/desktopPullRequestWrites";

import {
  EMPTY_REVIEW,
  landedReview,
  landedReviewCandidates,
  readReviewSubmit,
  reviewSubmitOwner,
  type SentReview,
  updateReviewSubmit,
  pendingSubmitScope,
  sameAttempt,
} from "./reviewSubmit";
import { pendingSummaryKey, pendingSummarySubmitted } from "./pendingReviewSummary";
import { reportReviewLandedByRead } from "./sendReview";
import { invalidatePendingReview } from "./usePendingReview";

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/**
 * One read of the viewer's reviews, as `from`'s account; null if it can't be had, or that
 * account signed in again or changed while it read.
 */
export async function readReviewCheck(
  prUrl: string,
  from: MacWriteRoute,
): Promise<ReviewCheck | null> {
  const { target, actor } = from;
  try {
    const { data, viewer: reader } = await readOnThisMac<unknown>(
      target,
      "pull_request_review_check",
      { repository: target.repository, number: target.number },
    );
    const parsed = parseReviewCheckAnswer(data);
    // Only an answer read as the account that would submit says anything about its reviews.
    const stillActing = macRouteCurrent(prUrl, from) !== null;
    return parsed && reader && sameLogin(reader, actor.login) && stillActing ? parsed : null;
  } catch {
    return null;
  }
}

/** What a Check Again came to; null where there was nothing to check, or it was replaced. */
export type ReviewCheckOutcome =
  | { kind: "landed" }
  | { kind: "not_found" | "unreadable" | "ambiguous"; check: ReviewCheck | null };

/**
 * Whether `key` still holds the uncertain review `sent`, the one a check was asked for, for the
 * workspace user signed in now: another one's review isn't theirs to check, whatever its login.
 */
function stillUncertain(key: string, sent: SentReview): boolean {
  if (reviewSubmitOwner(key) !== draftOwner()) return false;
  const status = readReviewSubmit(key).status;
  return status.kind === "uncertain" && sameAttempt(status.sent, sent);
}

/**
 * Check Again for the uncertain review in `key`: settles it once the read finds it, and reports
 * it to the page it was sent from. It never sends the review again.
 */
export async function checkReviewAgain({
  key,
  prUrl,
  queryClient,
  conversationId,
  sent: expected,
  showsHere,
}: {
  key: string;
  prUrl: string;
  queryClient: QueryClient;
  conversationId: string;
  /** The uncertain review it was offered for, by its send; a newer one has its own check. */
  sent?: SentReview;
  /** Whether the view it was asked from still shows it, for its toast; absent: always. */
  showsHere?: () => boolean;
}): Promise<ReviewCheckOutcome | null> {
  const entry = readReviewSubmit(key);
  const status = entry.status;
  if (status.kind !== "uncertain" || status.checking) return null;
  if (expected && !sameAttempt(status.sent, expected)) return null;
  const { sent } = status;
  if (!stillUncertain(key, sent)) return null;
  // In the scope it was sent in, whichever view shows it, under that login's sign-in now:
  // one since the send is fine, another login or scope can't say whether it landed.
  const sentAs = entry.sentAs;
  const from = sentAs
    ? macRouteAs(prUrl, { login: sentAs.actor.login, reader: sentAs.reader })
    : null;
  if (from) {
    updateReviewSubmit(key, (stored) =>
      stored.status.kind === "uncertain" && sameAttempt(stored.status.sent, sent)
        ? { ...stored, status: { ...stored.status, checking: true } }
        : stored,
    );
  }
  const check = from ? await readReviewCheck(prUrl, from) : null;
  if (!stillUncertain(key, sent)) {
    // Another workspace user signed in meanwhile: this one's review can be checked once back.
    updateReviewSubmit(key, (stored) =>
      stored.status.kind === "uncertain" && sameAttempt(stored.status.sent, sent)
        ? { ...stored, status: { ...stored.status, checking: false } }
        : stored,
    );
    return null;
  }
  const landed = check ? landedReview(sent, check) : null;
  if (landed) {
    updateReviewSubmit(key, () => EMPTY_REVIEW);
    reportReviewLandedByRead(key, sent.event);
    if (!showsHere || showsHere()) showToast("Your review is on GitHub.");
    void invalidateSessionPullRequest(queryClient, conversationId, prUrl, ["github-info"]);
    // A pending review's submit: its own summary, as sent, goes, and its drafts read again.
    const pending = pendingSubmitScope(key);
    const owner = reviewSubmitOwner(key);
    if (pending && owner && sentAs) {
      pendingSummarySubmitted(
        pendingSummaryKey(
          owner,
          prUrl,
          { login: sentAs.actor.login, namedReader: pending.reader?.login ?? null },
          pending.reviewId,
        ),
        sent.body,
      );
      invalidatePendingReview(queryClient, prUrl);
      void invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
        "github-review-threads",
      ]);
    }
    return { kind: "landed" };
  }
  // A send a reload cut off can't tell its review apart from another just like it.
  const checked = !check
    ? "unreadable"
    : landedReviewCandidates(sent, check).length > 1
      ? "ambiguous"
      : "not_found";
  updateReviewSubmit(key, (stored) =>
    stored.status.kind === "uncertain" && sameAttempt(stored.status.sent, sent)
      ? { ...stored, status: { ...stored.status, checking: false, checked } }
      : stored,
  );
  return { kind: checked, check };
}
