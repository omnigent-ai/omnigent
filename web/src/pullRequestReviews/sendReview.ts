// Sending a review through the session's host, for the Review menu: the review store tracks it, so
// the page lists, the menu and ⌘K agree on what's in flight, unconfirmed or refused, and the page
// it was sent from hears when GitHub made it.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { randomUUID } from "@/lib/randomUUID";
import type { QueryClient } from "@tanstack/react-query";

import { announceChange } from "@/components/pendingChange/announceChange";
import { showToast } from "@/components/ui/toast";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import type {
  PullRequestActionOrigin,
  PullRequestActionReport,
  PullRequestOwnAction,
} from "@/lib/pullRequestActionOrigin";

import {
  EMPTY_REVIEW,
  type ReviewEvent,
  forgetKeptReview,
  readReviewSubmit,
  updateReviewSubmit,
  type SentReview,
} from "./reviewSubmit";
import {
  type HostReviewRoute,
  hostReviewSubmittedMessage,
  rememberHostReviewBaseline,
  submitReviewOnHost,
} from "./hostReviews";

export const REVIEW_BUSY =
  "A review or comment on this pull request is still being sent. Wait for it to finish.";

function newAttemptId(): string {
  return randomUUID();
}

export const REVIEW_UNCONFIRMED =
  "GitHub didn’t confirm your last review. Check whether it was submitted before sending another.";

/** What each verdict did, for the Pull Requests page's history. */
const OWN_ACTION: Record<ReviewEvent, PullRequestOwnAction> = {
  APPROVE: "approved",
  REQUEST_CHANGES: "changes_requested",
  COMMENT: "commented",
};

/**
 * The page each unsettled submit was sent from, by review key, with the account it was sent
 * as: captured at the click, so a later Check Again from anywhere reports to the same page.
 */
const submitOrigins = new Map<string, PullRequestActionReport>();

/** Whether the review `key` sent, still unsettled, was sent from a page that keeps its outcome. */
export function reviewSentFromPage(key: string): boolean {
  return submitOrigins.has(key);
}

/** GitHub made the review `key` sent: tell the page it was sent from, once. */
export function reportReviewLanded(key: string, event: ReviewEvent): void {
  const report = submitOrigins.get(key);
  submitOrigins.delete(key);
  report?.succeeded(OWN_ACTION[event]);
}

/**
 * A read found the review `key` sent, after GitHub's answer couldn't say: tell the page it was
 * sent from, once, though its login may have signed in again since.
 */
export function reportReviewLandedByRead(key: string, event: ReviewEvent): void {
  const report = submitOrigins.get(key);
  submitOrigins.delete(key);
  if (report?.confirmedByRead) report.confirmedByRead(OWN_ACTION[event]);
  else report?.succeeded(OWN_ACTION[event]);
}

/** Why a send stopped: this window couldn't keep or clear its copy, so a reload could resend it. */
export const REVIEW_NOT_KEPT =
  "Couldn’t update the copy this window keeps of your review, so it wasn’t sent. Try again.";

/** Host reviews being sent, by review key: one at a time per PR and account. */
const hostSends = new Set<string>();

export interface SendReviewOnHostOptions {
  key: string;
  showsHere?: () => boolean;
  /** The session's host, with the account it reports for the PR (empty when it names none). */
  route: HostReviewRoute;
  prUrl: string;
  event: ReviewEvent;
  body: string;
  headSha: string;
  /** The account's latest review state the page showed before the send, for Check Again. */
  priorState: string | null;
  /** The pending review it finishes, sent with SubmitPendingReview; absent for a new review. */
  pendingReviewId?: string;
  origin: PullRequestActionOrigin | null;
  queryClient: QueryClient;
  onApplied?: () => void;
  onRefused?: (code: string, message: string) => void;
}

/**
 * Send `event` through the session's host: a comment review with SubmitReview, a verdict with
 * SubmitReviewVerdict. A lost answer holds it as unconfirmed, never sent again; Check Again
 * reads the PR through the host ({@link checkHostReviewAgain}).
 */
export function sendReviewOnHost({
  key,
  showsHere,
  route,
  prUrl,
  event,
  body,
  headSha,
  priorState,
  pendingReviewId,
  origin,
  queryClient,
  onApplied,
  onRefused,
}: SendReviewOnHostOptions): boolean {
  // A review GitHub didn't confirm stays held until a check settles it, whoever sends next.
  if (readReviewSubmit(key).status.kind === "uncertain") {
    onRefused?.("unconfirmed", REVIEW_UNCONFIRMED);
    return false;
  }
  if (hostSends.has(key)) {
    updateReviewSubmit(key, (stored) => ({
      ...stored,
      status: { kind: "refused", code: "busy", message: REVIEW_BUSY },
    }));
    return false;
  }
  const sent: SentReview = {
    event,
    body,
    head: headSha,
    knownReviewIds: null,
    sentAt: Date.now(),
    attemptId: newAttemptId(),
  };
  const held = updateReviewSubmit(key, (stored) => ({
    ...stored,
    event,
    body,
    status: { kind: "submitting", sent },
    sentAs: { actor: { ...route.actor }, reader: null },
  }));
  if (held === "failed" && forgetKeptReview(key) === "failed") {
    updateReviewSubmit(key, (stored) => ({
      ...stored,
      status: { kind: "refused", code: "not_kept", message: REVIEW_NOT_KEPT },
    }));
    onRefused?.("not_kept", REVIEW_NOT_KEPT);
    return false;
  }
  rememberHostReviewBaseline(sent, priorState);
  // Told only to the workspace user who sent it, while its menu still shows it.
  const sentBy = draftOwner();
  const tell = (show: () => void) => {
    if (draftOwner() === sentBy && (!showsHere || showsHere())) show();
  };
  const report = origin?.begin(prUrl, route.actor, { headSha });
  if (report) submitOrigins.set(key, report);
  else submitOrigins.delete(key);
  announceChange(event === "APPROVE" && !body ? "Approving…" : "Submitting your review…");
  hostSends.add(key);
  void submitReviewOnHost(route.conversationId, {
    prUrl,
    event,
    body,
    headSha,
    pendingReviewId,
  }).then(
    () => {
      hostSends.delete(key);
      updateReviewSubmit(key, () => EMPTY_REVIEW);
      reportReviewLanded(key, event);
      const where = `${route.target.repository}#${route.target.number}`;
      tell(() => showToast(hostReviewSubmittedMessage(event, where, route.actor.login)));
      void invalidateSessionPullRequest(queryClient, route.conversationId, prUrl, [
        "pr-page-github-info",
      ]);
      onApplied?.();
    },
    (error: unknown) => {
      hostSends.delete(key);
      if (error instanceof PullRequestWriteError && error.outcomeUnknown) {
        tell(() => announceChange(`GitHub didn’t confirm your review: ${error.message}`));
        updateReviewSubmit(key, (stored) => ({
          ...stored,
          status: {
            kind: "uncertain",
            sent,
            message: error.message,
            checking: false,
            checked: "never",
          },
        }));
        return;
      }
      submitOrigins.delete(key);
      const code = error instanceof PullRequestWriteError ? error.code : "rejected";
      const message =
        error instanceof Error && error.message ? error.message : "GitHub refused your review.";
      tell(() => announceChange(`Couldn’t submit your review: ${message}`));
      updateReviewSubmit(key, (stored) => ({
        ...stored,
        status: { kind: "refused", code, message },
      }));
      onRefused?.(code, message);
    },
  );
  return true;
}
