// The Review menu's controller: what it offers for a PR (a review on this Mac, a link to
// GitHub, or nothing), submitting one as the Mac account shown, and settling a submit GitHub
// didn't confirm only once a read shows the review it made.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { useMacReadAs } from "@/hooks/macReadAs";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  type PullRequestActionOrigin,
  usePullRequestActionOrigin,
  withRowLanding,
} from "@/lib/pullRequestActionOrigin";
import type { MacNamedReader } from "@/lib/macNamedReads";
import {
  macReviewWriteOffers,
  macWriteRoute,
  type MacWriteRoute,
  type ReviewCheck,
  useMacWriteRouteVersion,
  useWorkspaceDraftOwner,
} from "@/lib/desktopPullRequestWrites";

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
import { checkReviewAgain, readReviewCheck } from "./reviewCheck";
import { checkHostReviewAgain } from "./hostReviewCheck";
import { hostPendingIdentity, hostPendingRoute } from "./hostPendingRoute";
import { type HostReviewRoute, hostReviewRoute } from "./hostReviews";
import { REVIEW_UNCONFIRMED, sendReview, sendReviewOnHost } from "./sendReview";
import { editPendingSummary, pendingSummarySubmitted } from "./pendingReviewSummary";
import { type PendingFinish, pendingSubmitBlocked, usePendingFinish } from "./usePendingFinish";
import { invalidatePendingReview } from "./usePendingReview";

export type ReviewOffer =
  | { kind: "none" }
  /** This Mac can't submit here: review on the provider (`label`, e.g. GitHub) instead. */
  | { kind: "link"; url: string; label: string }
  | { kind: "mac"; route: MacWriteRoute }
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
  // The menu shows only for an open PR this Mac reviews as `viewer`; it sends its own draft.
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
  if (finish.unresolved) return PENDING_CHANGE_UNRESOLVED;
  if (!headSha || !finish.reviewCommit) return NO_HEAD;
  const verdict = verdictUnavailableReason(entry.event, viewer, author);
  if (verdict) return verdict;
  return pendingSubmitBlocked(entry.event, finish.summaryText, finish.total);
}

/**
 * What the Review menu offers for `pr`: a review on this Mac, through the session's host where
 * it serves reviews, a link to GitHub, or nothing.
 */
export function useReviewOffer(
  pr: Pick<ReviewPullRequest, "url" | "state">,
  providerLabel: string,
  host?: { conversationId: string; account: string | null | undefined },
): ReviewOffer {
  useMacWriteRouteVersion();
  const offer = reviewOfferFor(pr, useMacReadAs(), providerLabel);
  const sessionId = host?.conversationId ?? "";
  const comments = usePullRequestMethodServed(sessionId, "submitReview");
  const verdicts = usePullRequestMethodServed(sessionId, "submitReviewVerdict");
  if (offer.kind === "mac" || !host || !comments || pr.state?.toUpperCase() !== "OPEN") {
    return offer;
  }
  const route = hostReviewRoute(host.conversationId, pr.url, host.account);
  return route ? { kind: "host", route, verdicts: verdicts && !!route.actor.login } : offer;
}

/** The Review menu's offer for `pr` read as `readAs`, for a list row to offer the same. */
export function reviewOfferFor(
  pr: Pick<ReviewPullRequest, "url" | "state">,
  readAs: MacNamedReader | null,
  providerLabel: string,
): ReviewOffer {
  // GitHub takes reviews on open pull requests, drafts included.
  if (pr.state?.toUpperCase() !== "OPEN") return { kind: "none" };
  const route = macWriteRoute(pr.url, readAs);
  if (route && macReviewWriteOffers(route, "pull_request_review_submit")) {
    return { kind: "mac", route };
  }
  return /^https:\/\//.test(pr.url)
    ? { kind: "link", url: pr.url, label: `Review on ${providerLabel}` }
    : { kind: "none" };
}

export interface ReviewSubmitController {
  offer: ReviewOffer;
  /** This PR's review for the workspace user and Mac account shown, or null off this Mac. */
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
  /** The review check's last answer for this key, or null before one. */
  check: ReviewCheck | null;
  setEvent: (event: ReviewEvent) => void;
  setBody: (body: string) => void;
  /** Read whether a review is pending and where the head is, e.g. as the menu opens. */
  refreshCheck: () => void;
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

/** The menu's controller while `offer` is a review on this Mac. */
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
  const route = offer.kind === "mac" ? offer.route : null;
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
    route,
    hostRoute: pendingHostRoute,
    open,
  });
  // A pending review's submit keeps to its own scope and review, apart from a regular review.
  const key = route
    ? finish
      ? pendingSubmitKey(
          owner,
          pr.url,
          { login: route.actor.login, reader: route.reader },
          finish.reviewId,
        )
      : reviewSubmitKey(owner, pr.url, route.actor.login)
    : hostRoute
      ? finish
        ? pendingSubmitKey(
            owner,
            pr.url,
            // Each session's host apart: one host's unconfirmed submit never pins another's menu.
            { login: pendingHostRoute ? hostPendingIdentity(pendingHostRoute) : "", reader: null },
            finish.reviewId,
          )
        : reviewSubmitKey(owner, pr.url, hostRoute.actor.login)
      : null;
  const entry = useReviewSubmitEntry(key);
  const locked = entry.status.kind === "submitting" || entry.status.kind === "uncertain";
  const body = finish && !locked ? finish.summaryText : entry.body;
  const viewer = route ? route.actor.login : hostRoute?.actor.login || null;
  const [checked, setChecked] = useState<{ key: string; value: ReviewCheck } | null>(null);
  const [keepAsked, setKeepAsked] = useState<string | null>(null);
  const check = checked && checked.key === key ? checked.value : null;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refreshCheck = () => {
    if (!key || !route) return;
    const requestedFor = key;
    void readReviewCheck(pr.url, route).then((value) => {
      if (value && mounted.current) setChecked({ key: requestedFor, value });
    });
  };

  // Only a regular review's own hold: a pending review's text stays with its review on GitHub.
  const keepable = !!key && !finish && entry.status.kind === "uncertain" && !entry.status.checking;

  const checkAgain = () => {
    if (finish?.unresolved?.kind === "discard") {
      finish.checkDiscard();
      return;
    }
    if (key && hostRoute) {
      const forKey = key;
      void checkHostReviewAgain({
        key: forKey,
        route: hostRoute,
        prUrl: pr.url,
        queryClient,
        showsHere: () => mounted.current,
      });
      return;
    }
    if (!key || !route) return;
    const forKey = key;
    // Its toast shows only while this menu still shows this account's review.
    const token = finish?.viewToken;
    const showsHere = () => mounted.current && (!token || !finish || finish.viewCurrent(token));
    void checkReviewAgain({
      key: forKey,
      prUrl: pr.url,
      queryClient,
      conversationId,
      showsHere,
    }).then((outcome) => {
      if (outcome && outcome.kind !== "landed" && outcome.check && mounted.current) {
        setChecked({ key: forKey, value: outcome.check });
      }
    });
  };
  const refreshPending = () => {
    invalidatePendingReview(queryClient, pr.url);
    void invalidateSessionPullRequest(queryClient, conversationId, pr.url, [
      "github-review-threads",
    ]);
  };

  const submit = () => {
    if (key && hostRoute && finish) {
      submitPendingOnHost(key, hostRoute, finish);
      return;
    }
    if (key && hostRoute) {
      submitOnHost(key, hostRoute);
      return;
    }
    if (!key || !route) return;
    if (finish) {
      submitPending(key, route, finish);
      return;
    }
    const current = readReviewSubmit(key);
    const reason = submitUnavailableReason(current, {
      viewer: route.actor.login,
      author: pr.author,
      headSha: pr.headSha,
      pending: check?.pending === true,
    });
    if (reason || !pr.headSha) return;
    sendReview({
      key,
      route,
      prUrl: pr.url,
      event: current.event,
      body: current.body,
      headSha: pr.headSha,
      origin: originFor(origin, current.event),
      queryClient,
      conversationId,
      // A menu that has gone, e.g. as its PR left the page's list, has nothing to close.
      onApplied: () => {
        if (mounted.current) onSubmitted?.();
      },
      // A moved head or a review started elsewhere: read where the PR stands now.
      onRefused: (code) => {
        if (code === "head_changed" || code === "review_pending") refreshCheck();
      },
    });
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

  /** Finish the pending review, on its own commit, with the summary the menu shows. */
  const submitPending = (forKey: string, sendAs: MacWriteRoute, pending: PendingFinish) => {
    const current = readReviewSubmit(forKey);
    const reason = pendingFinishUnavailableReason(current, pending, {
      viewer: sendAs.actor.login,
      author: pr.author,
      headSha: pr.headSha,
    });
    if (reason || !pr.headSha || !pending.reviewCommit) return;
    const token = pending.viewToken;
    const sentSummary = pending.summaryText;
    const summaryKey = pending.summaryKey;
    sendReview({
      key: forKey,
      showsHere: () => mounted.current && pending.viewCurrent(token),
      route: sendAs,
      prUrl: pr.url,
      event: current.event,
      body: sentSummary,
      headSha: pr.headSha,
      pending: { reviewId: pending.reviewId, reviewCommit: pending.reviewCommit },
      origin: originFor(origin, current.event),
      queryClient,
      conversationId,
      onApplied: () => {
        // Its own summary and the reads settle wherever the page is; the menu closes only here.
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
    check,
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
    refreshCheck,
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
