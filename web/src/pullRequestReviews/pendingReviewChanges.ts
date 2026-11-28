// Changes to the account's pending review, sent as this Mac's account: adding, editing and
// deleting drafts, and discarding the review. A change GitHub didn't confirm is held for the
// workspace user who made it, until a read as the same account shows how it ended.

import { useSyncExternalStore } from "react";

import { type DraftAddAnswer, PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { draftOwner, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import {
  addDraftOnHost,
  deleteDraftOnHost,
  discardPendingReviewOnHost,
  editDraftOnHost,
  startPendingReviewOnHost,
} from "@/lib/hostPullRequestWrites";
import type { PendingDraftBaseline, PendingReviewStart } from "@/lib/nativeBridge";
import type { ReviewComment, ReviewThread } from "@/lib/pullRequestReviewThreads";
import { listCommentEdits } from "@/pullRequestComments/commentEditStore";

import {
  type HostPendingRoute,
  type HostPendingScope,
  type HostPendingStep,
  hostPendingIdentity,
  hostPendingOffers,
  hostPendingRoute,
  hostPendingScopeCurrent,
  hostPendingScopeOf,
} from "./hostPendingRoute";
import {
  type DraftPlace,
  listedDrafts,
  type PendingReviewRead,
  type SentDraft,
} from "./pendingReview";
import {
  draftAddVerdict,
  draftDeleteVerdict,
  draftEditVerdict,
  reviewStartVerdict,
} from "./pendingReviewEvidence";
import { PENDING_REVIEWS_UNAVAILABLE, pendingSubmitsOf } from "./reviewSubmit";
import { readPendingReviewOnHost } from "./usePendingReview";

export type { DraftPlace } from "./pendingReview";

/** One change to the pending review, as the page asks for it. */
export type PendingReviewChange =
  | {
      kind: "draft_add";
      body: string;
      place: DraftPlace;
      /** The pending review it joins; null to start one first. */
      pendingReviewId: string | null;
      /** The head the page shows; a push since refuses it. */
      expectedHead: string;
    }
  | { kind: "draft_edit"; commentId: string; reviewId: string; body: string; expected: string }
  | { kind: "draft_delete"; commentId: string; reviewId: string }
  | { kind: "discard"; reviewId: string };

type ChangeAnswer =
  | DraftAddAnswer<ReviewThread, ReviewComment>
  | { comment_id: string; body: string }
  | { comment_id: string }
  | { review_id: string };

/** Who a held change was sent as: the login and the session's host route it went along. */
export interface PendingSentAs {
  login: string;
  host: HostPendingScope;
}

/** A change GitHub may or may not have made, and what a later read needs to tell. */
export type PendingHold = {
  id: string;
  owner: string;
  prUrl: string;
  sentAs: PendingSentAs;
  message: string;
  checking: boolean;
  /** The last check couldn't read, or read nothing that settles it. */
  checked: "never" | "unreadable" | "unsettled";
} & (
  | {
      kind: "draft_add";
      body: string;
      /** Where it was sent, as sent: its lines and side, or the thread it replies in. */
      place: DraftPlace;
      /** What was in the draft's scope before; null when the shell couldn't say. */
      baseline: PendingDraftBaseline | null;
      /** A start GitHub didn't confirm; the draft itself wasn't sent then. */
      start: PendingReviewStart | null;
      /** The local draft it was sent from, so a confirmed add clears only that draft. */
      local: SentDraft | null;
    }
  | { kind: "draft_edit"; commentId: string; reviewId: string; body: string }
  | { kind: "draft_delete"; commentId: string; reviewId: string }
  | { kind: "discard"; reviewId: string }
);

/** What sending a change came to, for the composer or control that sent it. */
export type PendingChangeOutcome =
  | { kind: "done"; answer: ChangeAnswer; writer: string }
  /** Nothing changed: GitHub or main refused, or it couldn't be sent. */
  | { kind: "refused"; code: string; message: string }
  /** The review was started, but the draft wasn't added: add it to `reviewId` next. */
  | { kind: "partial"; reviewId: string; cause: string | null; message: string }
  /** GitHub may have made it: nothing more is sent for it until a check settles it. */
  | { kind: "held"; hold: PendingHold }
  /**
   * It answered after the account it was sent as stopped being the one the page writes as:
   * another login, a new sign-in or another scope. It says only how it ended, for that
   * account's own composer; nothing it read or wrote is shown in the page's account now.
   */
  | {
      kind: "moved";
      sentAs: { login: string; generation: number };
      settled: "done" | "partial" | "held" | "refused";
    }
  /** The workspace user changed meanwhile; nothing of it is this user's to show. */
  | { kind: "suppressed" };

export const PENDING_CHANGE_BUSY =
  "A review or comment on this pull request is still being sent. Wait for it to finish.";
export const PENDING_CHANGE_UNCONFIRMED = "Couldn’t confirm whether GitHub saved this.";
export const PENDING_REVIEW_END_UNRESOLVED =
  "Your review is being submitted or discarded, or GitHub didn’t confirm it. Check your review first.";
export const PENDING_DRAFTS_UNRESOLVED =
  "A comment in your review is still being saved, or GitHub didn’t confirm it. Check it first.";

// Held changes, by id; each shows only to its owner.
const holds = new Map<string, PendingHold>();
const listeners = new Set<() => void>();
let version = 0;
let holdSerial = 0;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

function putHold(hold: PendingHold): void {
  holds.set(hold.id, hold);
  changed();
}

/** What a held add's check needs: the drafts before it, or the start it began with. */
interface HoldDetails {
  draftBaseline?: PendingDraftBaseline;
  reviewStart?: PendingReviewStart;
}

function holdFor(
  owner: string,
  prUrl: string,
  sentAs: PendingSentAs,
  change: PendingReviewChange,
  details: HoldDetails,
  local: SentDraft | null,
): PendingHold {
  holdSerial += 1;
  const base = {
    id: `pending-hold-${holdSerial}`,
    owner,
    prUrl,
    sentAs,
    message: PENDING_CHANGE_UNCONFIRMED,
    checking: false,
    checked: "never" as const,
  };
  switch (change.kind) {
    case "draft_add":
      return {
        ...base,
        kind: "draft_add",
        body: change.body,
        place: change.place,
        baseline: details.draftBaseline ?? null,
        start: details.reviewStart ?? null,
        local,
      };
    case "draft_edit":
      return {
        ...base,
        kind: "draft_edit",
        commentId: change.commentId,
        reviewId: change.reviewId,
        body: change.body,
      };
    case "draft_delete":
      return {
        ...base,
        kind: "draft_delete",
        commentId: change.commentId,
        reviewId: change.reviewId,
      };
    case "discard":
      return { ...base, kind: "discard", reviewId: change.reviewId };
  }
}

const REVIEW_CHANGED_HERE =
  "Your pending review changed since this page read it. Refresh, then try again.";

// Pending-review changes sent through a host and not yet answered, by PR: one at a time.
const hostSending = new Set<string>();

/** Whether a pending-review change to `prUrl` is out through the session's host. */
export function hostPendingChangeInFlight(prUrl: string): boolean {
  return hostSending.has(prUrl.toLowerCase());
}

/** {@link hostPendingChangeInFlight}, re-read as changes start and end. */
export function useHostPendingChangeInFlight(prUrl: string | null): boolean {
  useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
  return !!prUrl && hostPendingChangeInFlight(prUrl);
}

const HOST_STEP: Record<PendingReviewChange["kind"], HostPendingStep> = {
  draft_add: "add",
  draft_edit: "edit",
  draft_delete: "delete",
  discard: "discard",
};

const outcomeUnknown = (error: unknown): boolean =>
  error instanceof PullRequestWriteError && error.outcomeUnknown;
const messageOf = (error: unknown): string =>
  error instanceof Error ? error.message : "GitHub refused this change.";

function baselineScope(place: DraftPlace): PendingDraftBaseline["scope"] {
  // Through the host, a reply's thread root isn't known, so a lost reply isn't told apart.
  return place.kind === "reply"
    ? { kind: "reply", threadId: place.threadId, rootCommentId: null }
    : { kind: "thread", path: place.path, line: place.line, startLine: place.startLine };
}

/**
 * The drafts already in `place`'s scope of `reviewId`, read through the host just before an add,
 * so an unconfirmed add can tell its own draft apart. A read that fails proves nothing.
 */
async function hostBaseline(
  route: HostPendingRoute,
  reviewId: string,
  place: DraftPlace,
): Promise<PendingDraftBaseline | "changed"> {
  const scope = baselineScope(place);
  try {
    const read = await readPendingReviewOnHost(route);
    if (read.review?.id !== reviewId) return "changed";
    const known = listedDrafts(read).filter((draft) =>
      scope.kind === "reply"
        ? false
        : draft.reply_to_id === null &&
          draft.path === scope.path &&
          draft.line === scope.line &&
          draft.start_line === scope.startLine,
    );
    return {
      reviewId,
      scope,
      complete: read.complete && scope.kind === "thread",
      knownCommentIds: known.map((draft) => draft.id),
    };
  } catch {
    return { reviewId, scope, complete: false, knownCommentIds: [] };
  }
}

/**
 * Send `change` through the session's host, along the route `route` reads the PR by. It's refused
 * without sending where that route no longer offers pending reviews, or a change is still in flight.
 * An add with no review starts one first, as its own step, so each lost step is held for what it is.
 */
export async function sendPendingReviewChange(
  prUrl: string,
  route: HostPendingRoute,
  change: PendingReviewChange,
  local: SentDraft | null = null,
): Promise<PendingChangeOutcome> {
  const starts = change.kind === "draft_add" && change.pendingReviewId === null;
  const steps: HostPendingStep[] = starts ? ["start", "add"] : [HOST_STEP[change.kind]];
  if (!steps.every((step) => hostPendingOffers(route, step))) {
    return {
      kind: "refused",
      code: "pending_reviews_unavailable",
      message: PENDING_REVIEWS_UNAVAILABLE,
    };
  }
  const flight = prUrl.toLowerCase();
  if (hostSending.has(flight)) {
    return { kind: "refused", code: "busy", message: PENDING_CHANGE_BUSY };
  }
  const owner = draftOwner();
  // No draft goes into a review being ended, and no review is discarded over a draft change.
  const identity = hostPendingIdentity(route);
  if (pendingReviewEndUnresolved(owner, prUrl, identity)) {
    return { kind: "refused", code: "review_unresolved", message: PENDING_REVIEW_END_UNRESOLVED };
  }
  if (change.kind === "discard" && pendingDraftChangeUnresolved(owner, prUrl, identity)) {
    return { kind: "refused", code: "drafts_unresolved", message: PENDING_DRAFTS_UNRESOLVED };
  }
  const sentAs: PendingSentAs = {
    login: route.actor.login,
    host: hostPendingScopeOf(route),
  };
  const sendingId = `host-${randomSendId()}`;
  hostSending.add(flight);
  if (change.kind === "discard") {
    discardsSending.set(sendingId, { owner, prUrl, sentAs, reviewId: change.reviewId });
  }
  changed();
  // The answer is the page's to show only for the user and route it was sent as.
  const settle = (outcome: Exclude<PendingChangeOutcome, { kind: "moved" | "suppressed" }>) => {
    if (draftOwner() !== owner) return { kind: "suppressed" } as const;
    if (hostPendingScopeCurrent(sentAs.host!, prUrl)) return outcome;
    const settled = outcome.kind === "done" ? "done" : outcome.kind;
    return {
      kind: "moved",
      sentAs: { login: sentAs.login, generation: 0 },
      settled,
    } as const;
  };
  const hold = (details: HoldDetails) => {
    const held = holdFor(owner, prUrl, sentAs, change, details, local);
    putHold(held);
    return settle({ kind: "held", hold: held });
  };
  const conversationId = route.conversationId;
  try {
    switch (change.kind) {
      case "draft_add": {
        let reviewId = change.pendingReviewId;
        let baseline: PendingDraftBaseline;
        if (reviewId === null) {
          try {
            reviewId = (
              await startPendingReviewOnHost(conversationId, {
                prUrl,
                commitOid: change.expectedHead,
              })
            ).review_id;
          } catch (error) {
            if (outcomeUnknown(error)) {
              return hold({
                reviewStart: { commitOid: change.expectedHead, priorPendingReviewId: null },
              });
            }
            return settle({ kind: "refused", code: "rejected", message: messageOf(error) });
          }
          baseline = {
            reviewId,
            scope: baselineScope(change.place),
            complete: true,
            knownCommentIds: [],
          };
        } else {
          const read = await hostBaseline(route, reviewId, change.place);
          if (read === "changed") {
            return settle({
              kind: "refused",
              code: "review_changed",
              message: REVIEW_CHANGED_HERE,
            });
          }
          baseline = read;
        }
        try {
          const answer = await addDraftOnHost<ReviewThread, ReviewComment>(conversationId, {
            prUrl,
            body: change.body,
            pendingReviewId: reviewId,
            expectedHead: change.expectedHead,
            place: change.place,
          });
          return settle({ kind: "done", answer, writer: route.actor.login });
        } catch (error) {
          if (outcomeUnknown(error)) return hold({ draftBaseline: baseline });
          // The review it started is still pending: the draft goes into it next.
          if (starts) {
            return settle({ kind: "partial", reviewId, cause: null, message: messageOf(error) });
          }
          return settle({ kind: "refused", code: "rejected", message: messageOf(error) });
        }
      }
      case "draft_edit":
      case "draft_delete":
      case "discard": {
        try {
          const answer =
            change.kind === "discard"
              ? await discardPendingReviewOnHost(conversationId, {
                  prUrl,
                  pendingReviewId: change.reviewId,
                })
              : change.kind === "draft_edit"
                ? await editDraftOnHost(conversationId, {
                    prUrl,
                    commentId: change.commentId,
                    pendingReviewId: change.reviewId,
                    body: change.body,
                    expected: change.expected,
                  })
                : await deleteDraftOnHost(conversationId, {
                    prUrl,
                    commentId: change.commentId,
                    pendingReviewId: change.reviewId,
                  });
          return settle({ kind: "done", answer, writer: route.actor.login });
        } catch (error) {
          if (outcomeUnknown(error)) return hold({});
          return settle({ kind: "refused", code: "rejected", message: messageOf(error) });
        }
      }
    }
  } finally {
    hostSending.delete(flight);
    discardsSending.delete(sendingId);
    changed();
  }
}

let sendSerial = 0;
const randomSendId = () => {
  sendSerial += 1;
  return String(sendSerial);
};

/** A discard sent and not yet answered: which review, for whom, sent as which login and scope. */
export interface SendingDiscard {
  owner: string;
  prUrl: string;
  sentAs: PendingSentAs;
  reviewId: string;
}

const discardsSending = new Map<string, SendingDiscard>();

/** `owner`'s discards on `prUrl` sent and not yet answered. */
export function pendingDiscardsSending(owner: string, prUrl: string): SendingDiscard[] {
  const wanted = prUrl.toLowerCase();
  return [...discardsSending.values()].filter(
    (sending) => sending.owner === owner && sending.prUrl.toLowerCase() === wanted,
  );
}

/** What a check of a held change came to. */
export type HoldCheckOutcome =
  | { kind: "landed"; commentId?: string }
  /** An unconfirmed start made the review; the draft itself still has to be added to it. */
  | { kind: "started"; reviewId: string }
  | { kind: "discarded" }
  | { kind: "not_discarded" }
  | { kind: "submitted_elsewhere" }
  | { kind: "unsettled" }
  | { kind: "unreadable" };

const settles = (verdict: { kind: string }) => verdict.kind !== "held";

/** What a check read as the hold's account: the pending review, as the host reads it. */
export interface HoldEvidence {
  kind: "pending_review";
  read: PendingReviewRead;
}

/** How what a check read settles `hold`, if it does; pure, for the check and tests. */
export function settleHold(hold: PendingHold, evidence: HoldEvidence): HoldCheckOutcome {
  // The host can't read a discard's review by its id, so only the host check settles one.
  if (hold.kind === "discard") return { kind: "unsettled" };
  const { read } = evidence;
  switch (hold.kind) {
    case "draft_add": {
      if (hold.start) {
        const started = reviewStartVerdict(hold.start, read);
        return started.kind === "started"
          ? { kind: "started", reviewId: started.reviewId! }
          : { kind: "unsettled" };
      }
      const verdict = draftAddVerdict(hold, read);
      return verdict.kind === "landed"
        ? { kind: "landed", commentId: verdict.commentId }
        : { kind: "unsettled" };
    }
    case "draft_edit":
      return settles(draftEditVerdict(hold, read)) ? { kind: "landed" } : { kind: "unsettled" };
    case "draft_delete":
      return settles(draftDeleteVerdict(hold, read)) ? { kind: "landed" } : { kind: "unsettled" };
  }
}

/**
 * Check Again for `holdId`: reads, as the login and scope it was sent as under that login's
 * sign-in now, the pending review, or for a discard the review by its id. The hold goes only
 * when that settles it; a read that fails or proves nothing keeps it, and nothing is resent.
 */
export async function checkPendingHold(holdId: string): Promise<HoldCheckOutcome | null> {
  const hold = holds.get(holdId);
  if (!hold || hold.checking || hold.owner !== draftOwner()) return null;
  return checkHostHold(hold, hold.sentAs.host);
}

/**
 * Check Again for a hold sent through the session's host: one read of the pending review along
 * the route it was sent on. The host can't read a review by its id, so a discard settles only
 * while the review is still pending; otherwise it stays held for a look on GitHub.
 */
async function checkHostHold(
  hold: PendingHold,
  scope: HostPendingScope,
): Promise<HoldCheckOutcome | null> {
  const route = hostPendingScopeCurrent(scope, hold.prUrl)
    ? hostPendingRoute(scope.conversationId, hold.prUrl)
    : null;
  if (!route) {
    putHold({ ...hold, checked: "unreadable" });
    return { kind: "unreadable" };
  }
  putHold({ ...hold, checking: true });
  let outcome: HoldCheckOutcome;
  try {
    const read = await readPendingReviewOnHost(route);
    outcome =
      hold.kind === "discard"
        ? read.review?.id === hold.reviewId
          ? { kind: "not_discarded" }
          : { kind: "unsettled" }
        : settleHold(hold, { kind: "pending_review", read });
  } catch {
    outcome = { kind: "unreadable" };
  }
  const now = holds.get(hold.id);
  if (!now) return null;
  if (now.owner !== draftOwner()) {
    putHold({ ...now, checking: false });
    return null;
  }
  if (outcome.kind === "unsettled" || outcome.kind === "unreadable") {
    putHold({ ...now, checking: false, checked: outcome.kind });
  } else {
    holds.delete(hold.id);
    changed();
  }
  return outcome;
}

/**
 * Whether `owner` holds a discard of a pending review on `prUrl` sent as `login`, in any scope:
 * a new review of it waits until that's checked.
 */
export function pendingDiscardHeld(owner: string, prUrl: string, login: string): boolean {
  const wanted = login.toLowerCase();
  return (
    pendingHoldsOf(owner, prUrl).some(
      (hold) => hold.kind === "discard" && hold.sentAs.login.toLowerCase() === wanted,
    ) ||
    pendingDiscardsSending(owner, prUrl).some(
      (sending) => sending.sentAs.login.toLowerCase() === wanted,
    )
  );
}

const sameIdentity = (scope: HostPendingScope | undefined, identity: string) =>
  !!scope && hostPendingIdentity(scope) === identity;

/**
 * Whether a draft add, edit or delete in `identity`'s pending review on `prUrl` is out, or GitHub
 * didn't confirm it: the review isn't submitted or discarded until that's settled.
 */
export function pendingDraftChangeUnresolved(
  owner: string,
  prUrl: string,
  identity: string,
): boolean {
  if (hostPendingChangeInFlight(prUrl)) return true;
  const held = pendingHoldsOf(owner, prUrl).some(
    (hold) => hold.kind !== "discard" && sameIdentity(hold.sentAs.host, identity),
  );
  return (
    held ||
    listCommentEdits(owner, prUrl).some(
      ({ entry }) =>
        (entry.pending !== null || entry.unknown !== null) &&
        sameIdentity(entry.privateScope?.host, identity),
    )
  );
}

/**
 * Whether a submit or discard of `identity`'s pending review on `prUrl` is out, or GitHub didn't
 * confirm it: no draft goes into the review until that's settled.
 */
export function pendingReviewEndUnresolved(
  owner: string,
  prUrl: string,
  identity: string,
): boolean {
  const login = identity.toLowerCase();
  return (
    pendingSubmitsOf(owner, prUrl).some((each) => each.login === login) ||
    pendingHoldsOf(owner, prUrl).some(
      (hold) => hold.kind === "discard" && sameIdentity(hold.sentAs.host, identity),
    ) ||
    pendingDiscardsSending(owner, prUrl).some((each) => sameIdentity(each.sentAs.host, identity))
  );
}

/** Stop holding a change its owner chose to check on GitHub instead. */
export function dismissPendingHold(holdId: string): void {
  const hold = holds.get(holdId);
  if (!hold || hold.owner !== draftOwner()) return;
  holds.delete(holdId);
  changed();
}

/** The workspace user's held changes on `prUrl`, newest last. */
export function pendingHoldsOf(owner: string, prUrl: string): PendingHold[] {
  const wanted = prUrl.toLowerCase();
  return [...holds.values()].filter(
    (hold) => hold.owner === owner && hold.prUrl.toLowerCase() === wanted,
  );
}

export function usePendingHolds(prUrl: string | null): PendingHold[] {
  const owner = useWorkspaceDraftOwner();
  useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
  return prUrl ? pendingHoldsOf(owner, prUrl) : [];
}

export function resetPendingReviewChangesForTests(): void {
  holds.clear();
  discardsSending.clear();
  hostSending.clear();
  holdSerial = 0;
  changed();
}
