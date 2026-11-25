// Changes to the account's pending review, sent as this Mac's account: adding, editing and
// deleting drafts, and discarding the review. A change GitHub didn't confirm is held for the
// workspace user who made it, until a read as the same account shows how it ended.

import { useSyncExternalStore } from "react";

import {
  beginMacWrite,
  draftOwner,
  type DraftAddAnswer,
  MacWriteError,
  macPendingReviewOffers,
  macRouteAs,
  macRouteCurrent,
  macWriteInFlight,
  macWriteMayPatch,
  type MacWriteRoute,
  macWriteRouteStill,
  PENDING_REVIEW_STATE_READ,
  type PendingReviewWriteOperation,
  parseCommentDeleteAnswer,
  parseCommentEditAnswer,
  parseDraftAddAnswer,
  parseReviewDiscardAnswer,
  sendMacWrite,
  settleMacWrite,
  useWorkspaceDraftOwner,
} from "@/lib/desktopPullRequestWrites";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  addDraftOnHost,
  deleteDraftOnHost,
  discardPendingReviewOnHost,
  editDraftOnHost,
  startPendingReviewOnHost,
} from "@/lib/hostPullRequestWrites";
import type { MacNamedReader } from "@/lib/macNamedReads";
import type { PendingDraftBaseline, PendingReviewStart } from "@/lib/nativeBridge";
import type { ReviewComment, ReviewThread } from "@/lib/pullRequestReviewThreads";

import {
  type HostPendingRoute,
  type HostPendingScope,
  type HostPendingStep,
  hostPendingOffers,
  hostPendingRoute,
  hostPendingScopeCurrent,
  hostPendingScopeOf,
  isHostPendingRoute,
} from "./hostPendingRoute";
import {
  type DraftPlace,
  listedDrafts,
  parseReviewStateAnswer,
  type SentDraft,
  type PendingReviewRead,
  type ReviewNodeState,
} from "./pendingReview";
import {
  discardVerdict,
  draftAddVerdict,
  draftDeleteVerdict,
  draftEditVerdict,
  reviewStartVerdict,
} from "./pendingReviewEvidence";
import { PENDING_REVIEWS_UNAVAILABLE } from "./reviewSubmit";
import { readPendingReview, readPendingReviewOnHost } from "./usePendingReview";

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

const OPERATION: Record<PendingReviewChange["kind"], PendingReviewWriteOperation> = {
  draft_add: "pull_request_review_draft_add",
  draft_edit: "pull_request_review_draft_edit",
  draft_delete: "pull_request_review_draft_delete",
  discard: "pull_request_review_discard",
};

function paramsOf(route: MacWriteRoute, change: PendingReviewChange): Record<string, unknown> {
  const pr = { repository: route.target.repository, number: route.target.number };
  switch (change.kind) {
    case "draft_add": {
      const { place } = change;
      return {
        ...pr,
        body: change.body,
        ...(place.kind === "lines"
          ? {
              anchor: {
                path: place.path,
                side: place.side,
                line: place.line,
                start_line: place.startLine,
                head_sha: place.headSha,
                base_sha: place.baseSha,
              },
            }
          : { thread_id: place.threadId }),
        pending_review_id: change.pendingReviewId,
        expected_head: change.expectedHead,
      };
    }
    case "draft_edit":
      return {
        ...pr,
        comment_id: change.commentId,
        pending_review_id: change.reviewId,
        body: change.body,
        expected: change.expected,
      };
    case "draft_delete":
      return { ...pr, comment_id: change.commentId, pending_review_id: change.reviewId };
    case "discard":
      return { ...pr, pending_review_id: change.reviewId };
  }
}

type ChangeAnswer =
  | DraftAddAnswer<ReviewThread, ReviewComment>
  | { comment_id: string; body: string }
  | { comment_id: string }
  | { review_id: string };

const PARSE: Record<PendingReviewChange["kind"], (data: unknown) => ChangeAnswer | null> = {
  draft_add: (data) => parseDraftAddAnswer<ReviewThread, ReviewComment>(data),
  draft_edit: parseCommentEditAnswer,
  draft_delete: parseCommentDeleteAnswer,
  discard: parseReviewDiscardAnswer,
};

/** Who a held change was sent as: a login, in the host's scope or a named one. */
export interface PendingSentAs {
  login: string;
  reader: MacNamedReader | null;
  /** Sent through the session's host, along this route, rather than as this Mac's account. */
  host?: HostPendingScope;
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
      sentAs: { login: string; generation: number; reader: MacNamedReader | null };
      settled: "done" | "partial" | "held" | "refused";
    }
  /** The workspace user changed meanwhile; nothing of it is this user's to show. */
  | { kind: "suppressed" };

export const PENDING_CHANGE_BUSY =
  "A review or comment on this pull request is still being sent. Wait for it to finish.";
export const PENDING_CHANGE_UNCONFIRMED = "Couldn’t confirm whether GitHub saved this.";

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

const macSentAs = (route: MacWriteRoute): PendingSentAs => ({
  login: route.actor.login,
  reader: route.reader,
});

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
  error instanceof MacWriteError && error.outcomeUnknown;
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
 * {@link sendPendingReviewChange} through the session's host. An add with no review starts one
 * first, as its own step, so a lost start and a lost add are each held for what they are; a
 * refused add after a start leaves the started review to add to.
 */
async function sendOnHost(
  prUrl: string,
  route: HostPendingRoute,
  change: PendingReviewChange,
  local: SentDraft | null,
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
  if (hostSending.has(flight) || macWriteInFlight(OPERATION[change.kind], route.target)) {
    return { kind: "refused", code: "busy", message: PENDING_CHANGE_BUSY };
  }
  const owner = draftOwner();
  const sentAs: PendingSentAs = {
    login: route.actor.login,
    reader: null,
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
      sentAs: { login: sentAs.login, generation: 0, reader: null },
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

/** The review a partial start made, from the step that started it. */
function startedReviewId(error: MacWriteError): string | null {
  const started = error.details.steps?.completed.find((step) => step.step === "start_review");
  return started?.review_id ?? null;
}

/**
 * Send `change` along the route `shown` (the account the control showed) now, from the click
 * itself. It's refused without sending where pending reviews aren't offered on that route
 * any more, the account moved, or a review change on this PR is still in flight.
 */
export function sendPendingReviewChange(
  prUrl: string,
  shown: Pick<MacWriteRoute, "actor" | "reader"> | HostPendingRoute,
  change: PendingReviewChange,
  /** For an add from a composer: the draft it's sent from. */
  local: SentDraft | null = null,
): Promise<PendingChangeOutcome> {
  if (isHostPendingRoute(shown)) return sendOnHost(prUrl, shown, change, local);
  const operation = OPERATION[change.kind];
  const route = macWriteRouteStill(prUrl, shown);
  if (route instanceof MacWriteError) {
    return Promise.resolve({ kind: "refused", code: route.code, message: route.message });
  }
  if (!macPendingReviewOffers(route)) {
    return Promise.resolve({
      kind: "refused",
      code: "pending_reviews_unavailable",
      message: PENDING_REVIEWS_UNAVAILABLE,
    });
  }
  if (macWriteInFlight(operation, route.target)) {
    return Promise.resolve({ kind: "refused", code: "busy", message: PENDING_CHANGE_BUSY });
  }
  const owner = draftOwner();
  const ticket = beginMacWrite(route);
  // A discard is known from the click until GitHub answers it, whatever reads say meanwhile.
  if (change.kind === "discard") {
    discardsSending.set(ticket.requestId, {
      owner,
      prUrl,
      sentAs: { login: route.actor.login, reader: route.reader },
      reviewId: change.reviewId,
    });
    changed();
  }
  const sent = sendMacWrite(ticket, operation, paramsOf(route, change), PARSE[change.kind], () =>
    // The workspace user changed before GitHub answered: held for the user who sent it.
    putHold(holdFor(owner, prUrl, macSentAs(route), change, {}, local)),
  );
  return new Promise<PendingChangeOutcome>((resolve) => {
    let settled = false;
    const answer = (outcome: PendingChangeOutcome) => {
      settled = true;
      resolve(outcome);
    };
    // Whether the answer is still the page's to show: the route the change went along is
    // still the one it writes along, the same login, sign-in and scope.
    const stillRouted = () => macRouteCurrent(prUrl, route) !== null;
    const moved = (ended: "done" | "partial" | "held" | "refused") =>
      answer({
        kind: "moved",
        sentAs: {
          login: route.actor.login,
          generation: route.actor.generation,
          reader: route.reader,
        },
        settled: ended,
      });
    void settleMacWrite(ticket, sent, {
      applied: ({ data, writer }) => {
        // What it made is read and patched only in the scope it was made in.
        if (!stillRouted() || !macWriteMayPatch(ticket, writer)) moved("done");
        else answer({ kind: "done", answer: data, writer });
      },
      failed: (error) => {
        const writeError = error instanceof MacWriteError ? error : null;
        if (writeError?.code === "partial") {
          const reviewId = startedReviewId(writeError);
          if (reviewId) {
            if (!stillRouted()) moved("partial");
            else {
              answer({
                kind: "partial",
                reviewId,
                cause: writeError.details.cause ?? null,
                message: writeError.message,
              });
            }
            return;
          }
        }
        if (!writeError || writeError.outcomeUnknown || writeError.code === "partial") {
          // Held for the login and scope it was sent as, whichever the page shows now.
          const hold = holdFor(
            owner,
            prUrl,
            macSentAs(route),
            change,
            writeError?.details ?? {},
            local,
          );
          putHold(hold);
          if (!stillRouted()) moved("held");
          else answer({ kind: "held", hold });
          return;
        }
        if (!stillRouted()) moved("refused");
        else answer({ kind: "refused", code: writeError.code, message: writeError.message });
      },
    }).finally(() => {
      if (discardsSending.delete(ticket.requestId)) changed();
      if (!settled) resolve({ kind: "suppressed" });
    });
  });
}

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

/** What a check read as the hold's account: its pending review, or for a discard the review by id. */
export type HoldEvidence =
  | { kind: "pending_review"; read: PendingReviewRead }
  | { kind: "review_state"; state: ReviewNodeState };

/** How what a check read settles `hold`, if it does; pure, for the check and tests. */
export function settleHold(hold: PendingHold, evidence: HoldEvidence): HoldCheckOutcome {
  if (hold.kind === "discard") {
    if (evidence.kind !== "review_state") return { kind: "unsettled" };
    const verdict = discardVerdict(hold.reviewId, evidence.state);
    if (verdict.kind !== "held") return { kind: verdict.kind };
    return { kind: evidence.state === "unreadable" ? "unreadable" : "unsettled" };
  }
  if (evidence.kind !== "pending_review") return { kind: "unsettled" };
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

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/**
 * The viewer's review `reviewId` read by its id along `route`, as that login in that scope;
 * `unreadable` for a shell without the read, another login's answer, or one that finished after
 * the route or workspace user moved.
 */
async function readReviewState(
  prUrl: string,
  route: MacWriteRoute,
  reviewId: string,
): Promise<ReviewNodeState> {
  const owner = draftOwner();
  const { data, viewer: login } = await readOnThisMac<unknown>(
    route.target,
    PENDING_REVIEW_STATE_READ,
    {
      repository: route.target.repository,
      number: route.target.number,
      review_id: reviewId,
    },
  );
  if (!login || !sameLogin(login, route.actor.login)) return "unreadable";
  if (draftOwner() !== owner || !macRouteCurrent(prUrl, route)) return "unreadable";
  return parseReviewStateAnswer(data, reviewId) ?? "unreadable";
}

/**
 * Check Again for `holdId`: reads, as the login and scope it was sent as under that login's
 * sign-in now, the pending review, or for a discard the review by its id. The hold goes only
 * when that settles it; a read that fails or proves nothing keeps it, and nothing is resent.
 */
export async function checkPendingHold(holdId: string): Promise<HoldCheckOutcome | null> {
  const hold = holds.get(holdId);
  if (!hold || hold.checking || hold.owner !== draftOwner()) return null;
  if (hold.sentAs.host) return checkHostHold(hold, hold.sentAs.host);
  const route = macRouteAs(hold.prUrl, hold.sentAs);
  if (!route) {
    putHold({ ...hold, checked: "unreadable" });
    return { kind: "unreadable" };
  }
  putHold({ ...hold, checking: true });
  let outcome: HoldCheckOutcome;
  try {
    const evidence: HoldEvidence =
      hold.kind === "discard"
        ? { kind: "review_state", state: await readReviewState(hold.prUrl, route, hold.reviewId) }
        : { kind: "pending_review", read: await readPendingReview(hold.prUrl, route) };
    outcome = settleHold(hold, evidence);
  } catch {
    outcome = { kind: "unreadable" };
  }
  const now = holds.get(holdId);
  if (!now) return null;
  // Another workspace user signed in meanwhile: nothing of it is applied, but its owner can
  // check again once back.
  if (now.owner !== draftOwner()) {
    putHold({ ...now, checking: false });
    return null;
  }
  if (outcome.kind === "unsettled" || outcome.kind === "unreadable") {
    putHold({ ...now, checking: false, checked: outcome.kind });
  } else {
    holds.delete(holdId);
    changed();
  }
  return outcome;
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
