// Sending a review as this Mac's account, for the Review menu and for a row's one-click
// Approve alike: the review store tracks it, so the page lists, the menu and ⌘K agree on what's
// in flight, unconfirmed or refused, and the page it was sent from hears when GitHub made it.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { randomUUID } from "@/lib/randomUUID";
import { type MacNamedReader, sameNamedReader } from "@/lib/macNamedReads";
import type { QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { announceChange } from "@/components/pendingChange/announceChange";
import { showToast } from "@/components/ui/toast";
import { subscribeIdentity } from "@/lib/identity";
import { guardedMacRetry, macSendScope } from "@/lib/macSendScope";
import {
  BULK_RETRY_LIMIT,
  type BulkRetry,
  bulkRetryAllowed,
  bulkRetryDelayMs,
} from "@/lib/macWriteRetries";
import { pendingDiscardHeld } from "./pendingReviewChanges";
import {
  beginMacWrite,
  draftOwner,
  MacWriteError,
  macPendingReviewOffers,
  macReviewWriteOffers,
  macWriteInFlight,
  macWriteRoute,
  type MacWriteRoute,
  macWriteRouteStill,
  parseReviewSubmitAnswer,
  sameMacRoute,
  sendMacWrite,
  settleMacWrite,
} from "@/lib/desktopPullRequestWrites";
import type {
  PullRequestActionOrigin,
  PullRequestActionReport,
  PullRequestOwnAction,
} from "@/lib/pullRequestActionOrigin";

import {
  EMPTY_REVIEW,
  type PendingReviewToFinish,
  type ReviewEvent,
  forgetKeptReview,
  type SentReview,
  readReviewSubmit,
  unsettledReviewEntry,
  reviewSubmitKey,
  updateReviewSubmit,
  PENDING_REVIEWS_UNAVAILABLE,
  type ReviewSubmitEntry,
} from "./reviewSubmit";
import { APPROVAL_SOURCES, type ApprovalSource } from "./approvalSources";
import {
  type HostReviewRoute,
  hostReviewSubmittedMessage,
  rememberHostReviewBaseline,
  submitReviewOnHost,
} from "./hostReviews";
import {
  APPROVE_HELD,
  APPROVE_STATE_UNKNOWN,
  approveUnavailableReason,
} from "./approveEligibility";

export const REVIEW_BUSY =
  "A review or comment on this pull request is still being sent. Wait for it to finish.";
export { PENDING_REVIEWS_UNAVAILABLE };

function newAttemptId(): string {
  return randomUUID();
}

const HELD_BY_DISCARD: ReviewSubmitEntry = { ...EMPTY_REVIEW, status: { kind: "submitting" } };

/**
 * What a new approval of `prUrl` as `login` must wait for: a review being sent or unconfirmed,
 * a pending review's included, or a discard of one not yet settled. Nothing of a pending
 * review's text is in it.
 */
export function approvalGuardEntry(owner: string, prUrl: string, login: string): ReviewSubmitEntry {
  return pendingDiscardHeld(owner, prUrl, login)
    ? HELD_BY_DISCARD
    : unsettledReviewEntry(owner, prUrl, login);
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

/** What submitting `event` said it did, for the toast. */
export function reviewSubmittedMessage(event: ReviewEvent, where: string, writer: string): string {
  if (event === "APPROVE") return `Approved ${where} as @${writer}.`;
  if (event === "REQUEST_CHANGES") return `Requested changes on ${where} as @${writer}.`;
  return `Submitted your review of ${where} as @${writer}.`;
}

export interface SendReviewOptions {
  /** The review store's key for this PR, workspace user and Mac account. */
  key: string;
  /**
   * Whether the view that sent it still shows it; its toasts and announcements wait on this,
   * while its own entry, report and refresh settle wherever the page is. Absent: always.
   */
  showsHere?: () => boolean;
  route: MacWriteRoute;
  prUrl: string;
  event: ReviewEvent;
  body: string;
  /** The commit the review is for; GitHub refuses it once the PR moved on. */
  headSha: string;
  /**
   * The account's pending review to finish, with the commit it was started on, which may be
   * older than `headSha`; absent for a review made at once.
   */
  pending?: PendingReviewToFinish;
  origin: PullRequestActionOrigin | null;
  queryClient: QueryClient;
  conversationId: string;
  /** GitHub made the review, as `writer`. Runs after the store and the page heard. */
  onApplied?: (writer: string) => void;
  /** GitHub refused it, with the refusal's code; the store already says so. */
  onRefused?: (code: string, message: string) => void;
  /**
   * Whether it went through isn't known: GitHub didn't confirm it, or the workspace user
   * changed before it answered (`abandoned`). The store holds it for Check Again.
   */
  onUncertain?: (outcome: { abandoned: boolean }) => void;
  /** A bulk run's own: send it again on the run's grant after a refusal for a passing reason. */
  retry?: ReviewRetry;
}

/** When a bulk run's review may be sent again, how it waits first, and as whose send. */
export type ReviewRetry = BulkRetry;

const waitFor = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

/**
 * Send `event` with `body` now, in the click's own task. False, with the store saying why and
 * the draft kept, when another review or comment on the PR is still being sent, or `route`'s
 * account can't submit it now.
 */
export function sendReview({
  key,
  showsHere,
  route: shown,
  prUrl,
  event,
  body,
  headSha,
  pending,
  origin,
  queryClient,
  conversationId,
  onApplied,
  onRefused,
  onUncertain,
  retry,
}: SendReviewOptions): boolean {
  // The menu's account must still be the one acting: never send it as another, or elsewhere.
  // Finishing a pending review needs pending reviews offered on that route too.
  const still = pending
    ? macWriteRouteStill(prUrl, shown)
    : macWriteRouteStill(prUrl, shown, (current) =>
        macReviewWriteOffers(current, "pull_request_review_submit"),
      );
  const route =
    pending && !(still instanceof MacWriteError) && !macPendingReviewOffers(still)
      ? new MacWriteError("pending_reviews_unavailable", PENDING_REVIEWS_UNAVAILABLE)
      : still;
  if (route instanceof MacWriteError) {
    announceChange(`Couldn’t submit your review: ${route.message}`);
    updateReviewSubmit(key, (stored) => ({
      ...stored,
      event,
      body,
      status: { kind: "refused", code: route.code, message: route.message },
    }));
    onRefused?.(route.code, route.message);
    return false;
  }
  if (macWriteInFlight("pull_request_review_submit", route.target)) {
    updateReviewSubmit(key, (stored) => ({
      ...stored,
      status: { kind: "refused", code: "busy", message: REVIEW_BUSY },
    }));
    return false;
  }
  // What's sent, kept on this device before it goes, so a reload before GitHub answers holds it.
  const attempt: SentReview = {
    event,
    body,
    head: headSha,
    knownReviewIds: null,
    sentAt: Date.now(),
    attemptId: newAttemptId(),
    ...(pending ? { pending } : {}),
  };
  const held = updateReviewSubmit(key, (stored) => ({
    ...stored,
    event,
    body,
    status: { kind: "submitting", sent: attempt },
    sentAs: { actor: { ...route.actor }, reader: route.reader },
  }));
  // A hold storage refused: the older saved copy goes first, or a reload could send it again.
  const kept = held === "failed" ? forgetKeptReview(key) : "held";
  if (kept === "failed") {
    if (!showsHere || showsHere())
      announceChange(`Couldn’t submit your review: ${REVIEW_NOT_KEPT}`);
    updateReviewSubmit(key, (stored) => ({
      ...stored,
      status: { kind: "refused", code: "not_kept", message: REVIEW_NOT_KEPT },
    }));
    onRefused?.("not_kept", REVIEW_NOT_KEPT);
    return false;
  }
  if (held === "failed" && (!showsHere || showsHere())) {
    announceChange(
      "This window couldn’t keep a copy of your review, so it won’t come back if the window reloads before GitHub answers.",
    );
  }
  startSending();
  return true;

  function startSending() {
    if (route instanceof MacWriteError) return;
    const report = origin?.begin(prUrl, route.actor, { headSha });
    if (report) submitOrigins.set(key, report);
    else submitOrigins.delete(key);
    announceChange(event === "APPROVE" && !body ? "Approving…" : "Submitting your review…");
    // What this send shows when it settles, only while the view it came from still shows it.
    const tell = (show: () => void) => {
      if (!showsHere || showsHere()) show();
    };
    const refuse = (code: string, message: string) => {
      submitOrigins.delete(key);
      tell(() => announceChange(`Couldn’t submit your review: ${message}`));
      updateReviewSubmit(key, (stored) => ({
        ...stored,
        status: { kind: "refused", code, message },
      }));
      onRefused?.(code, message);
    };
    /** One send, on a fresh ticket; a bulk run's own sends again after a passing refusal. */
    const sendAttempt = (retries: number) => {
      const ticket = beginMacWrite(route);
      // Taken before the send, so the review it makes can only be newer.
      const sentAt = Date.now();
      const attemptId = newAttemptId();
      const uncertain = (message: string, knownReviewIds: readonly string[] | null) =>
        updateReviewSubmit(key, (stored) => ({
          ...stored,
          status: {
            kind: "uncertain",
            sent: {
              event,
              body,
              head: headSha,
              knownReviewIds,
              sentAt,
              attemptId,
              ...(pending ? { pending } : {}),
            },
            message,
            checking: false,
            checked: "never",
          },
        }));
      const sent = sendMacWrite(
        ticket,
        "pull_request_review_submit",
        {
          repository: route.target.repository,
          number: route.target.number,
          event,
          body,
          expected_head: headSha,
          ...(pending
            ? { pending_review_id: pending.reviewId, review_commit: pending.reviewCommit }
            : {}),
        },
        parseReviewSubmitAnswer,
        // The workspace user changed before GitHub answered: keep what was sent, held.
        () => {
          uncertain(REVIEW_UNCONFIRMED, null);
          onUncertain?.({ abandoned: true });
        },
      );
      void settleMacWrite(ticket, sent, {
        applied: ({ writer }) => {
          updateReviewSubmit(key, () => EMPTY_REVIEW);
          reportReviewLanded(key, event);
          const where = `${route.target.repository}#${route.target.number}`;
          tell(() => showToast(reviewSubmittedMessage(event, where, writer)));
          // The reviewers' states and the PR's activity change with it.
          void invalidateSessionPullRequest(queryClient, conversationId, prUrl, ["github-info"]);
          onApplied?.(writer);
        },
        failed: (error) => {
          // Never sent again: GitHub may have made it, and a second review would be another one.
          if (error instanceof MacWriteError && error.outcomeUnknown) {
            tell(() => announceChange(`GitHub didn’t confirm your review: ${error.message}`));
            uncertain(error.message, error.knownReviewIds ?? null);
            onUncertain?.({ abandoned: false });
            return;
          }
          const code = error instanceof MacWriteError ? error.code : "rejected";
          const cause = error instanceof MacWriteError ? (error.details.cause ?? null) : null;
          const again =
            retry &&
            retries < BULK_RETRY_LIMIT &&
            bulkRetryAllowed({ code, cause }, { rechecksBeforeWrite: false }) &&
            retry.mayRetry();
          if (!again) {
            refuse(code, error.message);
            return;
          }
          // Still submitting: GitHub refused it for a passing reason, before applying anything.
          void (retry.sleep ?? waitFor)(bulkRetryDelayMs(code, retries + 1)).then(() => {
            const current = macWriteRouteStill(prUrl, shown, (now) =>
              macReviewWriteOffers(now, "pull_request_review_submit"),
            );
            if (
              current instanceof MacWriteError ||
              !sameMacRoute(current, route) ||
              !retry.mayRetry() ||
              macWriteInFlight("pull_request_review_submit", route.target)
            ) {
              refuse(code, error.message);
              return;
            }
            const resend = () => sendAttempt(retries + 1);
            if (retry.within) retry.within(resend);
            else resend();
          });
        },
      });
    };
    sendAttempt(0);
  }
}

/** Why a send stopped: this window couldn't keep or clear its copy, so a reload could resend it. */
export const REVIEW_NOT_KEPT =
  "Couldn’t update the copy this window keeps of your review, so it wasn’t sent. Try again.";

/** Whether the store's entry for `key` is free for a new send: nothing in flight or held. */
export function reviewSendable(key: string): boolean {
  const { status } = readReviewSubmit(key);
  return status.kind === "idle" || status.kind === "refused";
}

/** What a one-click Approve did: sent it, or left it to the Review menu to show why not. */
export type ApproveNowOutcome = "sent" | "open-menu";

/** A refused one-click Approve's Retry, by review key, while its refusal shows. */
const approveRetries = new Map<string, () => void>();

/** Retry for a refused one-click Approve, or undefined where a fresh send can't help. */
export function approveRetryFor(key: string): (() => void) | undefined {
  return readReviewSubmit(key).status.kind === "refused" ? approveRetries.get(key) : undefined;
}

/**
 * Refusals a plain resend would only repeat: the PR moved, a review is open on GitHub, or the
 * account to approve as can't be used now.
 */
const NOT_RETRYABLE = new Set([
  "head_changed",
  "review_pending",
  "busy",
  "blocked",
  "account_changed",
  "not_sent",
]);

interface ApprovalOptions {
  /** The workspace user approving, whose review store entry it uses. */
  owner: string;
  prUrl: string;
  /** The head commit it approves, as read when it was chosen; GitHub refuses it once moved. */
  headSha: string | null | undefined;
  /** The page's own login the PR is read as; null for the host's account. */
  readAs: MacNamedReader | null;
  origin: PullRequestActionOrigin | null;
  queryClient: QueryClient;
  conversationId: string;
  /** The live list it's sent from (APPROVAL_SOURCES), read again at every send and Retry. */
  source: string;
  /** A bulk run's own: sent again on its grant after a refusal for a passing reason. */
  retry?: ReviewRetry;
}

export const APPROVAL_LIST_GONE = "The list it was approved from isn’t open now.";
export const APPROVAL_READER_CHANGED = "The page reads GitHub as another account now.";

/** Why the list `options` came from can't send it now, or the PR's facts as it has them. */
function liveApprovalFacts(
  options: ApprovalOptions,
): { kind: "gone"; reason: string } | { kind: "live"; facts: ReturnType<ApprovalSource["facts"]> } {
  const live = APPROVAL_SOURCES.current(options.source);
  if (!live) return { kind: "gone", reason: APPROVAL_LIST_GONE };
  if (!sameNamedReader(live.reader, options.readAs)) {
    return { kind: "gone", reason: APPROVAL_READER_CHANGED };
  }
  return { kind: "live", facts: live.facts(options.prUrl) };
}

interface ApprovalHandlers {
  onApplied: () => void;
  /** GitHub refused it; `retry` where a fresh send could go through. */
  onRefused: (message: string, retry?: () => void) => void;
  onUncertain: (outcome: { abandoned: boolean }) => void;
}

/**
 * Send one approval with no comment now, as `readAs` routes it, where the shared rule allows
 * it; why not where it doesn't, with nothing sent. A refusal keeps a Retry that sends only as
 * this one went, and reads the PR's facts again.
 */
function sendApproval(
  options: ApprovalOptions,
  {
    resend,
    routeChanged,
    handlersFor,
  }: {
    resend: () => void;
    routeChanged?: () => void;
    /** What to do as it settles, once it's known to be sent, for the route it goes as. */
    handlersFor: (route: MacWriteRoute) => ApprovalHandlers;
  },
): { sent: true } | { sent: false; reason: string } {
  const { owner, prUrl, headSha, readAs, origin, queryClient, conversationId } = options;
  const live = liveApprovalFacts(options);
  if (live.kind === "gone") return { sent: false, reason: live.reason };
  const route = macWriteRoute(prUrl, readAs);
  const key = route ? reviewSubmitKey(owner, prUrl, route.actor.login) : null;
  const now = live.facts;
  const reason = approveUnavailableReason({
    mode: "no-comment",
    open: now?.open ?? null,
    author: now?.author,
    actor: route?.actor.login ?? null,
    reviewsOffered: !!route && macReviewWriteOffers(route, "pull_request_review_submit"),
    headSha,
    // A pending review's submit or discard not yet settled holds this approval too, unseen.
    entry: key && route ? approvalGuardEntry(owner, prUrl, route.actor.login) : null,
  });
  if (reason !== null || !route || !key || !headSha) {
    return { sent: false, reason: reason ?? APPROVE_STATE_UNKNOWN };
  }
  // A new attempt replaces an earlier one's Retry, which a later refusal mustn't bring back.
  approveRetries.delete(key);
  // Only for the same workspace user, signed in the same time, and the same Mac account.
  const guarded = guardedMacRetry(macSendScope(owner, route), prUrl, { resend, routeChanged });
  const retry = () => {
    approveRetries.delete(key);
    guarded();
  };
  const handlers = handlersFor(route);
  const sent = sendReview({
    key,
    route,
    prUrl,
    event: "APPROVE",
    body: "",
    headSha,
    origin,
    queryClient,
    conversationId,
    retry: options.retry,
    onApplied: () => handlers.onApplied(),
    onRefused: (code, message) => {
      const retryable = !NOT_RETRYABLE.has(code);
      if (retryable) approveRetries.set(key, retry);
      handlers.onRefused(message, retryable ? retry : undefined);
    },
    onUncertain: (outcome) => handlers.onUncertain(outcome),
  });
  return sent ? { sent: true } : { sent: false, reason: APPROVE_HELD };
}

export interface ApproveNowOptions extends ApprovalOptions {
  /** Open the PR's Review menu, where a draft, a held review or a refusal is shown. */
  openReview: () => void;
}

/**
 * The toast a one-click Approve shows off the Pull Requests page: progress, then the result,
 * a refusal with Retry, or an unconfirmed approval with a way to check it. It goes when the
 * workspace user changes, since none of it is the next user's.
 */
function approveToast(where: string) {
  const id = toast.loading(`Approving ${where}…`, { testId: "toast" });
  const user = draftOwner();
  const stopWatching = subscribeIdentity(() => {
    if (draftOwner() !== user) dismiss();
  });
  const dismiss = () => {
    stopWatching();
    toast.dismiss(id);
  };
  // Sonner removes a toast whose action ran without calling onDismiss: stop watching first.
  const lasting = (action?: { label: string; onClick: () => void }) => ({
    id,
    testId: "toast",
    closeButton: true,
    duration: Number.POSITIVE_INFINITY,
    action: action && {
      label: action.label,
      onClick: () => {
        stopWatching();
        action.onClick();
      },
    },
    onDismiss: stopWatching,
  });
  return {
    // The success toast names the account; it takes this one's place.
    applied: dismiss,
    abandoned: dismiss,
    refused: (message: string, retry?: () => void) =>
      toast.error(
        `Couldn’t approve ${where}: ${message}`,
        lasting(retry && { label: "Retry", onClick: retry }),
      ),
    unconfirmed: (openReview: () => void) =>
      toast.warning(
        `GitHub didn’t confirm approving ${where}. Open its review to check whether it went through.`,
        lasting({ label: "Open Review", onClick: openReview }),
      ),
  };
}

/**
 * Approve the PR with no comment, at once, as this Mac's account. Where the shared rule says it
 * can't go, nothing is sent: the caller opens the Review menu, which shows why. Off the Pull
 * Requests page, whose lists show the change and its refusal, a toast shows progress and the
 * result.
 */
export function approveNow(options: ApproveNowOptions): ApproveNowOutcome {
  const { origin, openReview } = options;
  const shown: { feedback: ReturnType<typeof approveToast> | null } = { feedback: null };
  const outcome = sendApproval(options, {
    resend: () => {
      if (approveNow(options) === "open-menu") openReview();
    },
    routeChanged: openReview,
    handlersFor: (route) => {
      const feedback = origin
        ? null
        : approveToast(`${route.target.repository}#${route.target.number}`);
      shown.feedback = feedback;
      return {
        onApplied: () => feedback?.applied(),
        onRefused: (message, retry) => feedback?.refused(message, retry),
        onUncertain: ({ abandoned }) => {
          if (abandoned) feedback?.abandoned();
          else feedback?.unconfirmed(openReview);
        },
      };
    },
  });
  // Another review or comment on it was still being sent: nothing went.
  if (!outcome.sent) shown.feedback?.abandoned();
  return outcome.sent ? "sent" : "open-menu";
}

/** A kept Retry that sent nothing: the page's listed refusal now says why, with no Retry. */
function retryNotSent(options: ApprovalOptions, reason: string) {
  const route = macWriteRoute(options.prUrl, options.readAs);
  if (!route) return;
  const key = reviewSubmitKey(options.owner, options.prUrl, route.actor.login);
  approveRetries.delete(key);
  updateReviewSubmit(key, (stored) =>
    stored.status.kind === "refused"
      ? { ...stored, status: { kind: "refused", code: "not_sent", message: reason } }
      : stored,
  );
}

/** How one PR's approval in a batch ended, as the batch counts it. */
export type BatchApproveOutcome = "applied" | "refused" | "uncertain" | "skipped";

/**
 * Approve one PR of a batch with no comment, as the page's account, resolving once it settles.
 * Where the shared rule says it can't go now, nothing is sent (`skipped`, with why). A refusal
 * and an unconfirmed approval stay in the review store, as one Approve's do.
 */
export function approveInBatch(
  options: ApprovalOptions & {
    /** Why it was left unsent at its turn. */
    onSkipped?: (reason: string) => void;
  },
): Promise<BatchApproveOutcome> {
  return new Promise((resolve) => {
    const outcome = sendApproval(options, {
      // The page lists the refusal; its Retry sends this one approval again, or says why not.
      resend: () =>
        void approveInBatch({
          ...options,
          onSkipped: (reason) => retryNotSent(options, reason),
        }),
      handlersFor: () => ({
        onApplied: () => resolve("applied"),
        onRefused: () => resolve("refused"),
        onUncertain: () => resolve("uncertain"),
      }),
    });
    if (!outcome.sent) {
      options.onSkipped?.(outcome.reason);
      resolve("skipped");
    }
  });
}

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
      void invalidateSessionPullRequest(queryClient, route.conversationId, prUrl, ["github-info"]);
      onApplied?.();
    },
    (error: unknown) => {
      hostSends.delete(key);
      if (error instanceof MacWriteError && error.outcomeUnknown) {
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
      const code = error instanceof MacWriteError ? error.code : "rejected";
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
