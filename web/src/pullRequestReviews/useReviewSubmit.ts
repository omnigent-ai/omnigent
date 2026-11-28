// The Review menu's controller: what it offers for a PR (a review through the session's host, a
// link to GitHub, or nothing), submitting one as the account the host reports, and settling a
// submit GitHub didn't confirm only once a read shows the review it made.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  type PullRequestActionOrigin,
  usePullRequestActionOrigin,
  withRowLanding,
} from "@/lib/pullRequestActionOrigin";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";

import {
  clearReviewRefusal,
  keepHeldReviewAsDraft,
  pendingSubmitKey,
  readReviewSubmit,
  type ReviewEvent,
  type ReviewSubmitEntry,
  reviewSubmitKey,
  summaryRequired,
  updateReviewSubmit,
  useReviewSubmitEntry,
  verdictUnavailableReason,
} from "./reviewSubmit";
import { approveUnavailableReason } from "./approveEligibility";
import { checkHostReviewAgain } from "./hostReviewCheck";
import { hostPendingIdentity, hostPendingRoute } from "./hostPendingRoute";
import { type HostReviewRoute, hostReviewRoute } from "./hostReviews";
import { REVIEW_UNCONFIRMED, sendReviewOnHost } from "./sendReview";
import {
  carryRegularSummary,
  editPendingSummary,
  pendingSummarySubmitted,
} from "./pendingReviewSummary";
import { type PendingFinish, pendingSubmitBlocked, usePendingFinish } from "./usePendingFinish";
import { invalidatePendingReview } from "./usePendingReview";
import { PENDING_DRAFTS_UNRESOLVED, pendingDraftChangeUnresolved } from "./pendingReviewChanges";

export type ReviewOffer =
  | { kind: "none" }
  /** The host can't submit here: review on the provider (`label`, e.g. GitHub) instead. */
  | { kind: "link"; url: string; label: string }
  /**
   * The session's host sends it, as the account it reports. `verdicts` where this caller may
   * approve or request changes there (the owner, with the account known); else comments only.
   */
  | { kind: "host"; route: HostReviewRoute; verdicts: boolean };

export interface ReviewPullRequest {
  url: string;
  number: number;
  state: string | null | undefined;
  author?: string | null;
  headSha?: string | null;
  /** The host account's latest review state on the PR, as the page shows it. */
  hostReviewState?: string | null;
}

const NO_HEAD = "The app doesn’t know which commit you’re looking at. Refresh, then try again.";

/** Why the menu's Submit can't run now, or null when it can. */
export function submitUnavailableReason(
  entry: ReviewSubmitEntry,
  {
    viewer,
    author,
    headSha,
    pending,
  }: { viewer: string; author?: string | null; headSha?: string | null; pending: boolean },
): string | null {
  if (entry.status.kind === "uncertain") return REVIEW_UNCONFIRMED;
  if (entry.status.kind === "submitting") return "Your review is being submitted.";
  if (pending) return "Finish your review in progress on GitHub first.";
  if (!headSha) return NO_HEAD;
  // The menu shows only for an open PR the host's account reviews as `viewer`.
  const verdict =
    entry.event === "APPROVE"
      ? approveUnavailableReason({
          mode: "with-body",
          open: true,
          author,
          actor: viewer,
          reviewsOffered: true,
          headSha,
          entry,
        })
      : verdictUnavailableReason(entry.event, viewer, author);
  if (verdict) return verdict;
  if (summaryRequired(entry.event) && !entry.body.trim()) return "Write a summary first.";
  return null;
}

const ALL_EVENTS: readonly ReviewEvent[] = ["COMMENT", "APPROVE", "REQUEST_CHANGES"];
const COMMENT_ONLY: readonly ReviewEvent[] = ["COMMENT"];

export const PENDING_CHANGE_UNRESOLVED = "Check your last review change first.";
/** Through the session's host, finishing a pending review is the session owner's. */
export const PENDING_SUBMIT_OWNERS =
  "Only the session’s owner can submit this review. Finish it on GitHub instead.";

/** Why finishing the pending review can't run now, or null when it can. */
export function pendingFinishUnavailableReason(
  entry: ReviewSubmitEntry,
  finish: PendingFinish,
  { viewer, author, headSha }: { viewer: string; author?: string | null; headSha?: string | null },
): string | null {
  if (entry.status.kind === "uncertain") return REVIEW_UNCONFIRMED;
  if (entry.status.kind === "submitting") return "Your review is being submitted.";
  if (!finish.submittable) return PENDING_SUBMIT_OWNERS;
  if (finish.unresolved?.kind === "discarding") return "Your review is being discarded.";
  if (finish.unresolved?.kind === "drafts") return PENDING_DRAFTS_UNRESOLVED;
  if (finish.unresolved) return PENDING_CHANGE_UNRESOLVED;
  if (!headSha || !finish.reviewCommit) return NO_HEAD;
  const verdict = verdictUnavailableReason(entry.event, viewer, author);
  if (verdict) return verdict;
  return pendingSubmitBlocked(entry.event, finish.summaryText, finish.total);
}

/**
 * What the Review menu offers for `pr`: a review through the session's host where it serves
 * reviews, a link to GitHub, or nothing.
 */
export function useReviewOffer(
  pr: Pick<ReviewPullRequest, "url" | "state">,
  providerLabel: string,
  host?: { conversationId: string; account: string | null | undefined },
): ReviewOffer {
  const offer = reviewOfferFor(pr, providerLabel);
  const sessionId = host?.conversationId ?? "";
  const comments = usePullRequestMethodServed(sessionId, "submitReview");
  const verdicts = usePullRequestMethodServed(sessionId, "submitReviewVerdict");
  if (!host || !comments || pr.state?.toUpperCase() !== "OPEN") return offer;
  const route = hostReviewRoute(host.conversationId, pr.url, host.account);
  return route ? { kind: "host", route, verdicts: verdicts && !!route.actor.login } : offer;
}

/** The Review menu's offer for `pr` without a host route, for a list row to offer the same. */
export function reviewOfferFor(
  pr: Pick<ReviewPullRequest, "url" | "state">,
  providerLabel: string,
): ReviewOffer {
  // GitHub takes reviews on open pull requests, drafts included.
  if (pr.state?.toUpperCase() !== "OPEN") return { kind: "none" };
  return /^https:\/\//.test(pr.url)
    ? { kind: "link", url: pr.url, label: `Review on ${providerLabel}` }
    : { kind: "none" };
}

export interface ReviewSubmitController {
  offer: ReviewOffer;
  /** This PR's review for the workspace user and host account shown, or null without a host. */
  key: string | null;
  entry: ReviewSubmitEntry;
  /** The summary the editor shows: a pending review's own, or what was sent while it's held. */
  body: string;
  /** The account's pending review, finished from this menu; null for a review made at once. */
  finish: PendingFinish | null;
  viewer: string | null;
  author: string | null;
  headSha: string | null;
  /** The verdicts this menu offers: all three, or only a comment review through an editor's host. */
  events: readonly ReviewEvent[];
  setEvent: (event: ReviewEvent) => void;
  setBody: (body: string) => void;
  submit: () => void;
  /** After an unconfirmed submit: read the viewer's reviews and settle it if it's there. */
  checkAgain: () => void;
  /**
   * After an unconfirmed regular submit: turn what was sent back into a draft, once the person
   * confirms GitHub doesn't show it. Never sends anything.
   */
  keepAsDraft: {
    available: boolean;
    asked: boolean;
    ask: () => void;
    confirm: () => void;
    cancel: () => void;
  };
  /** Clear a refusal's message once the person has seen it. */
  clearRefusal: () => void;
}

/** An approval of this one PR shows where its row went on the Pull Requests page. */
function originFor(
  origin: PullRequestActionOrigin | null,
  event: ReviewEvent,
): PullRequestActionOrigin | null {
  return event === "APPROVE" ? withRowLanding(origin, "approved") : origin;
}

/** The menu's controller while `offer` is a review through the session's host or none. */
export function useReviewSubmit({
  conversationId,
  pr,
  offer,
  open = false,
  onSubmitted,
}: {
  conversationId: string;
  pr: ReviewPullRequest;
  offer: ReviewOffer;
  /** The menu is open, so a pending review's summary is seeded from GitHub's. */
  open?: boolean;
  /** GitHub made the review: e.g. close the menu, as it has nothing left to show. */
  onSubmitted?: () => void;
}): ReviewSubmitController {
  const owner = useWorkspaceDraftOwner();
  const queryClient = useQueryClient();
  const origin = usePullRequestActionOrigin();
  const hostRoute = offer.kind === "host" ? offer.route : null;
  const events: readonly ReviewEvent[] =
    offer.kind === "host" && !offer.verdicts ? COMMENT_ONLY : ALL_EVENTS;
  // Where the Review menu goes through the host, so does its pending review, once served.
  const hostPending = usePullRequestMethodServed(conversationId, "getPendingReview");
  const pendingHostRoute =
    hostRoute && hostPending ? hostPendingRoute(conversationId, pr.url) : null;
  const finish = usePendingFinish({
    conversationId,
    prUrl: pr.url,
    hostRoute: pendingHostRoute,
    open,
  });
  // A pending review's submit keeps to its own scope and review, apart from a regular review.
  // Each session's host apart: one host's unconfirmed submit never pins another's menu.
  const key = hostRoute
    ? finish
      ? pendingSubmitKey(
          owner,
          pr.url,
          { login: pendingHostRoute ? hostPendingIdentity(pendingHostRoute) : "" },
          finish.reviewId,
        )
      : reviewSubmitKey(owner, pr.url, hostRoute.actor.login)
    : null;
  const entry = useReviewSubmitEntry(key);
  // The summary and verdict written before the first inline comment started a pending review
  // move into it, once it's read, so Finish submits them.
  const regularKey = hostRoute ? reviewSubmitKey(owner, pr.url, hostRoute.actor.login) : null;
  const carryInto =
    finish && !finish.unresolved && finish.githubBody !== null && key && regularKey
      ? {
          regularKey,
          pendingKey: key,
          summaryKey: finish.summaryKey,
          githubBody: finish.githubBody,
        }
      : null;
  useEffect(() => {
    if (carryInto) carryRegularSummary(carryInto);
    // Carried again only when the review, its scope or GitHub's summary changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [carryInto?.regularKey, carryInto?.pendingKey, carryInto?.summaryKey, carryInto?.githubBody]);
  const locked = entry.status.kind === "submitting" || entry.status.kind === "uncertain";
  const body = finish && !locked ? finish.summaryText : entry.body;
  const viewer = hostRoute?.actor.login || null;
  const [keepAsked, setKeepAsked] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Only a regular review's own hold: a pending review's text stays with its review on GitHub.
  const keepable = !!key && !finish && entry.status.kind === "uncertain" && !entry.status.checking;

  const checkAgain = () => {
    if (finish?.unresolved?.kind === "discard") {
      finish.checkDiscard();
      return;
    }
    if (key && hostRoute) {
      void checkHostReviewAgain({
        key,
        route: hostRoute,
        prUrl: pr.url,
        queryClient,
        showsHere: () => mounted.current,
      });
    }
  };
  const refreshPending = () => {
    invalidatePendingReview(queryClient, pr.url);
    void invalidateSessionPullRequest(queryClient, conversationId, pr.url, [
      "pr-page-github-review-threads",
    ]);
  };

  const submit = () => {
    if (key && hostRoute && finish) {
      submitPendingOnHost(key, hostRoute, finish);
      return;
    }
    if (key && hostRoute) {
      submitOnHost(key, hostRoute);
    }
  };

  /** Send the menu's review through the session's host, as the account it reports. */
  const submitOnHost = (forKey: string, sendAs: HostReviewRoute) => {
    const current = readReviewSubmit(forKey);
    if (!events.includes(current.event)) return;
    const reason = submitUnavailableReason(current, {
      viewer: sendAs.actor.login,
      author: pr.author,
      headSha: pr.headSha,
      pending: false,
    });
    if (reason || !pr.headSha) return;
    sendReviewOnHost({
      key: forKey,
      showsHere: () => mounted.current,
      route: sendAs,
      prUrl: pr.url,
      event: current.event,
      body: current.body,
      headSha: pr.headSha,
      priorState: pr.hostReviewState ?? null,
      origin: originFor(origin, current.event),
      queryClient,
      onApplied: () => {
        if (mounted.current) onSubmitted?.();
      },
    });
  };

  /** Finish the pending review through the session's host, where its list serves the submit. */
  const submitPendingOnHost = (forKey: string, sendAs: HostReviewRoute, pending: PendingFinish) => {
    const current = readReviewSubmit(forKey);
    if (!pending.submittable) return;
    const reason = pendingFinishUnavailableReason(current, pending, {
      viewer: sendAs.actor.login,
      author: pr.author,
      headSha: pr.headSha,
    });
    if (reason || !pr.headSha) return;
    // Decided again at the click: a draft change sent since keeps the review from going.
    const identity = pendingHostRoute ? hostPendingIdentity(pendingHostRoute) : null;
    if (!identity || pendingDraftChangeUnresolved(owner, pr.url, identity)) return;
    const token = pending.viewToken;
    const sentSummary = pending.summaryText;
    const summaryKey = pending.summaryKey;
    sendReviewOnHost({
      key: forKey,
      showsHere: () => mounted.current && pending.viewCurrent(token),
      route: sendAs,
      prUrl: pr.url,
      event: current.event,
      body: sentSummary,
      headSha: pr.headSha,
      priorState: pr.hostReviewState ?? null,
      pendingReviewId: pending.reviewId,
      origin: originFor(origin, current.event),
      queryClient,
      onApplied: () => {
        pendingSummarySubmitted(summaryKey, sentSummary);
        refreshPending();
        if (mounted.current && pending.viewCurrent(token)) onSubmitted?.();
      },
      onRefused: () => refreshPending(),
    });
  };

  const editable = (stored: ReviewSubmitEntry) =>
    stored.status.kind === "idle" || stored.status.kind === "refused";
  // Changing what's sent clears the last refusal's message.
  const idle = { kind: "idle" } as const;
  return {
    offer,
    key,
    entry,
    body,
    finish,
    viewer,
    author: pr.author ?? null,
    headSha: pr.headSha ?? null,
    events,
    setEvent: (event) => {
      if (!key || !events.includes(event) || verdictUnavailableReason(event, viewer, pr.author)) {
        return;
      }
      updateReviewSubmit(key, (stored) =>
        editable(stored) ? { ...stored, event, status: idle } : stored,
      );
    },
    setBody: (text) => {
      if (!key) return;
      // A pending review's summary is its own, kept per scope and review.
      if (finish) {
        if (editable(readReviewSubmit(key))) editPendingSummary(finish.summaryKey, text);
        return;
      }
      updateReviewSubmit(key, (stored) =>
        editable(stored) ? { ...stored, body: text, status: idle } : stored,
      );
    },
    submit,
    checkAgain,
    keepAsDraft: {
      available: keepable,
      asked: keepable && keepAsked === key,
      ask: () => {
        if (keepable) setKeepAsked(key);
      },
      confirm: () => {
        setKeepAsked(null);
        if (key && keepable) void keepHeldReviewAsDraft(key);
      },
      cancel: () => setKeepAsked(null),
    },
    clearRefusal: () => {
      if (key) clearReviewRefusal(key);
    },
  };
}
