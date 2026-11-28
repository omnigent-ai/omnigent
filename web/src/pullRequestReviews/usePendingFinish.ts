// Finishing or discarding the host account's pending review from the Review menu: what it says
// about the review, its summary, and the Discard confirmation. A submit or discard GitHub didn't
// confirm comes first, pinned to its review and scope, whatever a later read says.

import {
  invalidateSessionPullRequest,
  isSessionPullRequestRead,
  pullRequestReadRestAt,
} from "@/lib/pullRequestRecord";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { showToast } from "@/components/ui/toast";
import type { PullRequestCommitsResult } from "@/newPullRequestPage/hooks/usePullRequests";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { pendingScopeCurrent } from "@/pullRequestComments/commentEditScope";
import { useCommentEditsFor } from "@/pullRequestComments/commentEditStore";

import { composerViewCurrent, mountComposerView, noteComposerView } from "./pendingComposer";
import {
  checkPendingHold,
  pendingDiscardsSending,
  pendingDraftChangeUnresolved,
  type PendingHold,
  sendPendingReviewChange,
  usePendingHolds,
} from "./pendingReviewChanges";
import {
  keepMyPendingSummary,
  openPendingSummary,
  pendingSummaryDiscarded,
  pendingSummaryKey,
  reconcilePendingSummary,
  takeGitHubsPendingSummary,
  usePendingSummary,
} from "./pendingReviewSummary";
import { pendingSubmitsOf, type ReviewEvent, useReviewSubmitsVersion } from "./reviewSubmit";
import { type HostPendingRoute, hostPendingIdentity, hostPendingOffers } from "./hostPendingRoute";
import { invalidatePendingReview, usePendingReview } from "./usePendingReview";

type HeldDiscard = Extract<PendingHold, { kind: "discard" }>;

/** The review's place behind the head, and how many commits came since, where that's known. */
export interface FinishOutdated {
  reviewCommit: string;
  head: string;
  newer: number | null;
}

export interface PendingFinish {
  reviewId: string;
  /** The commit the review was started on, which it's submitted on; null until it's read. */
  reviewCommit: string | null;
  /** GitHub's count of the review's comments, and how many the read listed; null unread. */
  total: number | null;
  shown: number | null;
  outdated: FinishOutdated | null;
  /** A submit or discard of this review GitHub didn't confirm, which comes before anything else. */
  unresolved:
    | { kind: "submit" }
    | { kind: "discarding" }
    | { kind: "discard"; hold: HeldDiscard }
    /** A draft add, edit or delete that's out, or GitHub didn't confirm. */
    | { kind: "drafts" }
    | null;
  summaryKey: string;
  summaryText: string;
  /** The summary GitHub has for the review; null until it's read. */
  githubBody: string | null;
  /** GitHub's summary changed after the person edited theirs. */
  conflict: boolean;
  takeGitHubs: () => void;
  keepMine: () => void;
  /** Whether this caller may submit it here: through the host, only where it serves the submit. */
  submittable: boolean;
  /** Whether this caller may discard it here, as with `submittable`. */
  discardable: boolean;
  /** The Discard confirmation, pinned to the review and scope it opened on. */
  discard: {
    asked: boolean;
    ask: () => void;
    cancel: () => void;
    confirm: () => void;
    sending: boolean;
  };
  checkDiscard: () => void;
  /** What the last discard or its check came to, for this view. */
  notice: string | null;
  /** Whether an answer from `token`'s view may still change what this view shows. */
  viewToken: { key: string; epoch: number };
  viewCurrent: (token: { key: string; epoch: number }) => boolean;
}

/** Why a pending review's submit can't go with this verdict and summary, matching the shell. */
export function pendingSubmitBlocked(
  event: ReviewEvent,
  body: string,
  total: number | null,
): string | null {
  if (total === null) return "Couldn’t count this review’s comments. Finish it on GitHub.";
  const written = body.trim() !== "";
  if (event === "REQUEST_CHANGES" && !written) return "Write a summary first.";
  if (event === "COMMENT" && !written && total === 0) return "Write a summary first.";
  return null;
}

const DISCARDED = "Review discarded.";
const NOT_DISCARDED = "Your review wasn’t discarded. It’s still pending.";
const SUBMITTED_ELSEWHERE = "This review was submitted elsewhere.";

/**
 * The menu's pending review, for the host's account, while the session's host serves pending
 * reviews and either a review is pending or a change to one is unresolved; null otherwise, which
 * leaves the menu as today's.
 */
export function usePendingFinish({
  conversationId,
  prUrl,
  hostRoute = null,
  open,
}: {
  conversationId: string;
  prUrl: string;
  /** The session's host route the pending review is read and changed along, where it serves. */
  hostRoute?: HostPendingRoute | null;
  /** The menu is open, so its summary is seeded once from GitHub's. */
  open: boolean;
}): PendingFinish | null {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  useReviewSubmitsVersion();
  const holds = usePendingHolds(prUrl);
  // Re-read as a draft's edit or delete is sent and answered.
  useCommentEditsFor(owner, prUrl);
  const offered = !!hostRoute;
  const { state } = usePendingReview(prUrl, offered, conversationId);
  // The session and route stand for the account the host doesn't name.
  const scope: { login: string } | null = hostRoute
    ? { login: hostPendingIdentity(hostRoute) }
    : null;

  // The operation first: an unconfirmed submit, or a held discard, in this scope.
  const unresolvedSubmit = scope
    ? (pendingSubmitsOf(owner, prUrl).find((each) => each.login === scope.login.toLowerCase()) ??
      null)
    : null;
  // A discard is this view's only when sent as its account and scope, through the same host.
  const sentInScope = (sentAs: PendingHold["sentAs"]) =>
    !!scope && hostPendingIdentity(sentAs.host) === scope.login;
  const heldDiscard =
    (holds.find(
      (hold): hold is HeldDiscard =>
        hold.kind === "discard" &&
        sentInScope(hold.sentAs) &&
        pendingScopeCurrent(hold.sentAs, prUrl),
    ) as HeldDiscard | undefined) ?? null;
  const sendingDiscard =
    pendingDiscardsSending(owner, prUrl).find((each) => sentInScope(each.sentAs)) ?? null;
  const draftsUnresolved = !!scope && pendingDraftChangeUnresolved(owner, prUrl, scope.login);
  const pendingNow = state.kind === "pending" ? state.review : null;
  const reviewId =
    unresolvedSubmit?.scope.reviewId ??
    sendingDiscard?.reviewId ??
    heldDiscard?.reviewId ??
    pendingNow?.id ??
    null;
  const review = pendingNow && pendingNow.id === reviewId ? pendingNow : null;
  const summaryKey =
    scope && reviewId
      ? pendingSummaryKey(owner, prUrl, { login: scope.login, namedReader: null }, reviewId)
      : null;
  const summary = usePendingSummary(summaryKey);
  const [conflict, setConflict] = useState<{ key: string; body: string } | null>(null);
  const [asked, setAsked] = useState<{ reviewId: string; scopeKey: string } | null>(null);
  const [notice, setNotice] = useState<{ key: string; text: string } | null>(null);

  // The view this menu shows the review in; a change's answer applies to it only here.
  const scopeKey = scope ? [owner, prUrl.toLowerCase(), scope.login.toLowerCase()].join("\n") : "";
  // The menu's view is its account and scope: each change carries its own review and summary,
  // and settles them wherever the page is, while what this view shows changes only here.
  const menuKey = ["pending-review-menu", owner, prUrl.toLowerCase()];
  const viewKey = scope && offered ? scopeKey : null;
  useEffect(() => {
    if (!viewKey) return;
    return mountComposerView(menuKey, viewKey);
    // The view's identity is its key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey]);
  const viewToken = viewKey ? noteComposerView(menuKey, viewKey) : { key: "", epoch: 0 };

  // A confirmation belongs to the review and scope it was asked for: any change ends it for
  // good, and coming back needs a new ask.
  useEffect(() => {
    if (asked && (asked.reviewId !== reviewId || asked.scopeKey !== scopeKey)) setAsked(null);
  }, [asked, reviewId, scopeKey]);

  const githubBody = review?.body ?? null;
  // Seeded once when the menu opens on this review; GitHub's later changes are reconciled.
  useEffect(() => {
    if (!open || !summaryKey || githubBody === null) return;
    openPendingSummary(summaryKey, githubBody);
    const { conflict: changed } = reconcilePendingSummary(summaryKey, githubBody);
    setConflict(changed ? { key: summaryKey, body: githubBody } : null);
  }, [open, summaryKey, githubBody]);

  if (!offered || !scope || !reviewId || !summaryKey) return null;

  const refresh = () => {
    invalidatePendingReview(queryClient, prUrl);
    void invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
      "pr-page-github-review-threads",
    ]);
    void invalidateSessionPullRequest(queryClient, conversationId, prUrl, ["pr-page-github-info"]);
  };
  const viewCurrent = (token: { key: string; epoch: number }) =>
    composerViewCurrent(menuKey, token);
  const say = (token: { key: string; epoch: number }, text: string) => {
    if (viewCurrent(token)) setNotice({ key: summaryKey, text });
  };

  /** How a discard ended, from its answer or a check: only an actual discard drops the summary. */
  const settledDiscard = (
    kind: "discarded" | "not_discarded" | "submitted_elsewhere",
    token: { key: string; epoch: number },
    forSummary: string,
  ) => {
    refresh();
    if (kind === "discarded") {
      pendingSummaryDiscarded(forSummary);
      // Told, not closed: the menu goes on to whatever is pending next, or today's Review.
      if (viewCurrent(token)) showToast(DISCARDED);
      return;
    }
    // Told as a toast too: with the review gone, the menu may no longer be finishing it.
    const text = kind === "submitted_elsewhere" ? SUBMITTED_ELSEWHERE : NOT_DISCARDED;
    say(token, text);
    if (viewCurrent(token)) showToast(text);
  };

  const pinnedHere = (pin: { reviewId: string; scopeKey: string } | null) =>
    !!pin && pin.reviewId === reviewId && pin.scopeKey === scopeKey;

  // Commits since the review's own, where the page has the PR's commit list at its head.
  const head = state.kind === "pending" ? state.read.head_sha : null;
  const reviewCommit = review?.commit_oid ?? null;
  let outdated: FinishOutdated | null = null;
  if (review && reviewCommit && head && reviewCommit !== head) {
    const listed = queryClient
      .getQueriesData<PullRequestCommitsResult>({
        predicate: ({ queryKey }) =>
          queryKey[0] === "pr-page-github-pr-commits" &&
          isSessionPullRequestRead(queryKey, conversationId, prUrl) &&
          queryKey[pullRequestReadRestAt(queryKey)] === head,
      })
      .map(([, data]) => data)
      .find((data) => !!data && !data.has_more);
    const at = listed ? listed.data.findIndex((commit) => commit.sha === reviewCommit) : -1;
    outdated = {
      reviewCommit,
      head,
      newer: listed && at >= 0 ? listed.data.length - at - 1 : null,
    };
  }

  return {
    reviewId,
    reviewCommit,
    total: review?.comment_count ?? null,
    shown: review?.read_count ?? null,
    outdated,
    unresolved: unresolvedSubmit
      ? { kind: "submit" }
      : sendingDiscard
        ? { kind: "discarding" }
        : heldDiscard
          ? { kind: "discard", hold: heldDiscard }
          : draftsUnresolved
            ? { kind: "drafts" }
            : null,
    summaryKey,
    summaryText: summary?.text ?? githubBody ?? "",
    githubBody,
    submittable: !!hostRoute && hostPendingOffers(hostRoute, "submit"),
    discardable: !!hostRoute && hostPendingOffers(hostRoute, "discard"),
    conflict: !!conflict && conflict.key === summaryKey,
    takeGitHubs: () => {
      if (!conflict || conflict.key !== summaryKey) return;
      takeGitHubsPendingSummary(summaryKey, conflict.body);
      setConflict(null);
    },
    keepMine: () => {
      if (!conflict || conflict.key !== summaryKey) return;
      keepMyPendingSummary(summaryKey, conflict.body);
      setConflict(null);
    },
    discard: {
      asked: pinnedHere(asked),
      ask: () => {
        if (unresolvedSubmit || sendingDiscard || heldDiscard || draftsUnresolved || !review) {
          return;
        }
        setAsked({ reviewId, scopeKey });
      },
      cancel: () => setAsked(null),
      confirm: () => {
        // Pinned to what it opened on: another review or scope now refuses rather than retargets.
        const pin = asked;
        setAsked(null);
        const blocked = unresolvedSubmit || sendingDiscard || heldDiscard || draftsUnresolved;
        if (!pinnedHere(pin) || !hostRoute || blocked) return;
        const token = viewToken;
        const forSummary = summaryKey;
        void sendPendingReviewChange(prUrl, hostRoute, { kind: "discard", reviewId }).then(
          (outcome) => {
            if (outcome.kind === "done") settledDiscard("discarded", token, forSummary);
            else if (outcome.kind === "refused") say(token, outcome.message);
            // Held: the hold shows its own Check again. Moved or suppressed: nothing shows.
            else if (outcome.kind === "held") refresh();
          },
        );
      },
      sending: !!sendingDiscard,
    },
    checkDiscard: () => {
      if (!heldDiscard) return;
      const token = viewToken;
      const forSummary = summaryKey;
      void checkPendingHold(heldDiscard.id).then((outcome) => {
        if (!outcome) return;
        if (
          outcome.kind === "discarded" ||
          outcome.kind === "not_discarded" ||
          outcome.kind === "submitted_elsewhere"
        ) {
          settledDiscard(outcome.kind, token, forSummary);
        }
      });
    },
    notice: notice && notice.key === summaryKey ? notice.text : null,
    viewToken,
    viewCurrent,
  };
}
