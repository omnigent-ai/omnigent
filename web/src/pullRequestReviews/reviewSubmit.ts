// Submitting a pull request review from the Review menu: the verdicts, who may pick which,
// what each needs, and the per-account state of one PR's review (its draft, a submit on its
// way, and one GitHub didn't confirm). No React here; the menu and ⌘K read the same rules.

import { useSyncExternalStore } from "react";
import { z } from "zod";

import type { CheckedReview, ReviewCheck } from "@/lib/pullRequestWriteAnswers";
import {
  CONTROL_REQUEST_WAIT_MS,
  type ControlPlace,
  requestPullRequestControl,
  resetControlRequestsForTests,
  takeControlRequest,
  useControlRequestSerial,
} from "@/pullRequestPage/controlRequests";

import { createWindowDrafts, type DraftWriteOutcome } from "./reviewDraftStorage";

export type ReviewEvent = "COMMENT" | "APPROVE" | "REQUEST_CHANGES";

export interface ReviewVerdict {
  event: ReviewEvent;
  title: string;
  detail: string;
}

export const REVIEW_VERDICTS: readonly ReviewVerdict[] = [
  { event: "COMMENT", title: "Comment", detail: "Share feedback without approving." },
  {
    event: "APPROVE",
    title: "Approve",
    detail: "Give feedback and approve merging these changes.",
  },
  {
    event: "REQUEST_CHANGES",
    title: "Request Changes",
    detail: "Ask for changes that must be addressed before merging.",
  },
];

/** The state GitHub gives the review each event submits. */
export const SUBMITTED_STATE: Record<ReviewEvent, string> = {
  COMMENT: "COMMENTED",
  APPROVE: "APPROVED",
  REQUEST_CHANGES: "CHANGES_REQUESTED",
};

export const OWN_PULL_REQUEST_REASON = "You can’t do this on your own pull request.";
export const OWN_APPROVAL_REASON = "You can’t approve your own pull request.";

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/** Why `event` can't be picked by `viewer` on a PR by `author`, or null when it can. */
export function verdictUnavailableReason(
  event: ReviewEvent,
  viewer: string | null | undefined,
  author: string | null | undefined,
): string | null {
  // GitHub keeps authors from approving or requesting changes on their own pull requests.
  if (event !== "COMMENT" && viewer && author && sameLogin(viewer, author)) {
    return event === "APPROVE" ? OWN_APPROVAL_REASON : OWN_PULL_REQUEST_REASON;
  }
  return null;
}

/** Approving needs no words; a comment or a request for changes does. */
export function summaryRequired(event: ReviewEvent): boolean {
  return event !== "APPROVE";
}

/** A pending review a submit finishes, on the commit it was started on. */
export interface PendingReviewToFinish {
  reviewId: string;
  reviewCommit: string;
}

/** What a submit sent, kept while GitHub hasn't confirmed it. */
export interface SentReview {
  event: ReviewEvent;
  body: string;
  /** The head commit the review was pinned to. */
  head: string;
  /** The viewer's reviews before the submit; null when main couldn't say. */
  knownReviewIds: readonly string[] | null;
  /** When it was sent, by this Mac's clock, in milliseconds. */
  sentAt: number;
  /** The pending review it finished; absent for a review made at once. */
  pending?: PendingReviewToFinish;
  /** This send's own id, which stays when a list sees it masked, so a check targets it alone. */
  attemptId?: string;
}

/** Whether two sent reviews are the same send: the same id, or the same one where none. */
export function sameAttempt(left: SentReview, right: SentReview): boolean {
  return left === right || (!!left.attemptId && left.attemptId === right.attemptId);
}

/** How far GitHub's clock may run behind this Mac's when it stamps a review it just made. */
export const REVIEW_CLOCK_SKEW_MS = 2 * 60_000;

/** Text as GitHub stores it, for telling a sent body from one read back. */
export const normalizedBody = (text: string) => text.replace(/\r\n/g, "\n").trim();

/**
 * The review `sent` made, if `check` shows it: one of the viewer's reviews that wasn't there
 * before and was made after the click, with its verdict, commit and summary. Nothing else
 * counts, not a pending review that went away nor an older review of the viewer's that
 * comes back into view, so the hold stays until this is seen.
 */
export function landedReview(sent: SentReview, check: ReviewCheck): CheckedReview | null {
  const found = landedReviewCandidates(sent, check);
  // Without the reviews from before the send, as after a reload cut it off, only one will do.
  if (!sent.pending && !sent.knownReviewIds) return found.length === 1 ? found[0] : null;
  return found[0] ?? null;
}

/**
 * Every review in `check` that could be the one `sent` made. Without the viewer's reviews from
 * before the send, more than one means it can't say which, so the hold stays.
 */
export function landedReviewCandidates(sent: SentReview, check: ReviewCheck): CheckedReview[] {
  // A finished pending review keeps its id, and its own commit, whatever the head is now.
  if (sent.pending) {
    const { reviewId, reviewCommit } = sent.pending;
    return check.reviews.filter(
      (review) =>
        review.id === reviewId &&
        review.state === SUBMITTED_STATE[sent.event] &&
        review.commit_oid === reviewCommit &&
        normalizedBody(review.body) === normalizedBody(sent.body),
    );
  }
  const known = new Set(sent.knownReviewIds ?? []);
  const madeAfterClick = (createdAt: string | null) => {
    const time = createdAt ? Date.parse(createdAt) : Number.NaN;
    return Number.isFinite(time) && time >= sent.sentAt - REVIEW_CLOCK_SKEW_MS;
  };
  return check.reviews.filter(
    (review) =>
      !known.has(review.id) &&
      madeAfterClick(review.created_at) &&
      review.state === SUBMITTED_STATE[sent.event] &&
      review.commit_oid === sent.head &&
      normalizedBody(review.body) === normalizedBody(sent.body),
  );
}

export type ReviewSubmitStatus =
  | { kind: "idle" }
  /** `sent` once it's on its way to GitHub, so a reload before an answer holds it. */
  | { kind: "submitting"; sent?: SentReview }
  /** GitHub refused, or main did before sending: nothing changed. */
  | { kind: "refused"; code: string; message: string }
  /** GitHub may have made the review: nothing more is sent until a check shows it. */
  | {
      kind: "uncertain";
      sent: SentReview;
      message: string;
      checking: boolean;
      /**
       * The last Check Again couldn't read, didn't find the review, found several that could be
       * it, as after a reload cut the send off, or saw only the account's state for a review with
       * a summary, which the host doesn't tie to a review.
       */
      checked: "never" | "unreadable" | "not_found" | "ambiguous" | "unidentified";
    };

/** The login and sign-in a review was sent as. */
export interface ReviewSentAs {
  actor: { login: string; generation: number };
}

export interface ReviewSubmitEntry {
  event: ReviewEvent;
  body: string;
  status: ReviewSubmitStatus;
  /**
   * Who its last submit was sent as: the login and its sign-in. Only that can say whether it
   * landed. Absent before one.
   */
  sentAs?: ReviewSentAs;
}

export const EMPTY_REVIEW: ReviewSubmitEntry = Object.freeze({
  event: "COMMENT",
  body: "",
  status: { kind: "idle" },
}) as ReviewSubmitEntry;

/**
 * One PR's review for one workspace user and Mac account: switching either shows a review of
 * its own, so a summary or a held submit never follows into another account.
 */
export function reviewSubmitKey(owner: string, prUrl: string, login: string): string {
  const key = [owner, prUrl.toLowerCase(), login.toLowerCase()].join("\n");
  keyParts.set(key, { owner, prUrl, login: login.toLowerCase() });
  return key;
}

/** A pending review's submit: private to the review it was written in. */
export interface PendingSubmitScope {
  reviewId: string;
}

const keyParts = new Map<
  string,
  { owner: string; prUrl: string; login: string; pending?: PendingSubmitScope }
>();

/**
 * One pending review's submit for one workspace user, account and review: apart from that
 * account's regular review, and from any other review's.
 */
export function pendingSubmitKey(
  owner: string,
  prUrl: string,
  scope: { login: string },
  reviewId: string,
): string {
  const login = scope.login.toLowerCase();
  // The host's scope, which keys every pending review this window kept.
  const key = [owner, prUrl.toLowerCase(), login, "pending", "host", reviewId].join("\n");
  keyParts.set(key, { owner, prUrl, login, pending: { reviewId } });
  return key;
}

/** The scope and review a pending submit's key names; null for a regular review's key. */
export function pendingSubmitScope(key: string): PendingSubmitScope | null {
  ensureLoaded();
  return keyParts.get(key)?.pending ?? null;
}

/** What a list or guard may see of a pending submit: its state, never what was written. */
function masked(entry: ReviewSubmitEntry): ReviewSubmitEntry {
  const { status } = entry;
  return {
    event: entry.event,
    body: "",
    status:
      status.kind === "uncertain"
        ? { ...status, sent: { ...status.sent, body: "" }, message: PENDING_SUBMIT_UNCONFIRMED }
        : status.kind === "refused"
          ? { ...status, message: PENDING_SUBMIT_REFUSED }
          : status,
    ...(entry.sentAs ? { sentAs: entry.sentAs } : {}),
  };
}

/** Why a pending review change can't go: pending reviews aren't offered on this route now. */
export const PENDING_REVIEWS_UNAVAILABLE =
  "Pending reviews aren’t available here right now. Nothing was changed.";

export const PENDING_SUBMIT_UNCONFIRMED =
  "GitHub didn’t confirm your pending review was submitted.";
const PENDING_SUBMIT_REFUSED = "GitHub refused your pending review.";

/**
 * `owner`'s unsettled pending submits on `prUrl` as `login`, in any scope or review, with what
 * they hold. For the menu that shows them in their own scope, never for a list.
 */
export function pendingSubmitsOf(
  owner: string,
  prUrl: string,
): { key: string; login: string; scope: PendingSubmitScope; entry: ReviewSubmitEntry }[] {
  ensureLoaded();
  const wanted = prUrl.toLowerCase();
  const found: {
    key: string;
    login: string;
    scope: PendingSubmitScope;
    entry: ReviewSubmitEntry;
  }[] = [];
  for (const [key, entry] of entries) {
    const parts = keyParts.get(key);
    if (!parts?.pending || parts.owner !== owner || parts.prUrl.toLowerCase() !== wanted) continue;
    const { kind } = entry.status;
    if (kind === "submitting" || kind === "uncertain") {
      found.push({ key, login: parts.login, scope: parts.pending, entry });
    }
  }
  return found;
}

/**
 * The entry a new review of `prUrl` as `login` must wait for: the regular one, unless a pending
 * submit in any scope is in flight or unconfirmed, which shows only masked.
 */
export function unsettledReviewEntry(
  owner: string,
  prUrl: string,
  login: string,
): ReviewSubmitEntry {
  const regular = readReviewSubmit(reviewSubmitKey(owner, prUrl, login));
  const held = pendingSubmitsOf(owner, prUrl).find((each) => each.login === login.toLowerCase());
  return held ? masked(held.entry) : regular;
}

/** The workspace user `key`'s review belongs to; null for a key never made here. */
export function reviewSubmitOwner(key: string): string | null {
  ensureLoaded();
  return keyParts.get(key)?.owner ?? null;
}
const entries = new Map<string, ReviewSubmitEntry>();
const listeners = new Set<() => void>();
let version = 0;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** Shown on a review a reload cut off before GitHub answered. */
export const REVIEW_INTERRUPTED =
  "A reload interrupted this review before GitHub answered. Check whether it was submitted before sending another.";

// What this window keeps per review across a reload: an unsent draft, or a submit still held
// until a check or the person settles it.
const eventSchema = z.enum(["COMMENT", "APPROVE", "REQUEST_CHANGES"]);
const sentSchema = z.object({
  event: eventSchema,
  body: z.string(),
  head: z.string(),
  knownReviewIds: z.array(z.string()).readonly().nullable(),
  sentAt: z.number(),
  pending: z.object({ reviewId: z.string(), reviewCommit: z.string() }).optional(),
  attemptId: z.string().optional(),
});
const keptSchema = z.object({
  owner: z.string(),
  prUrl: z.string(),
  login: z.string(),
  // Records kept with a page login's `reader` still parse: zod drops the unknown key.
  pending: z.object({ reviewId: z.string() }).optional(),
  event: eventSchema,
  body: z.string(),
  /** A submit on its way or unconfirmed: what was sent, and what the hold says. */
  held: z.object({ sent: sentSchema, message: z.string() }).optional(),
  sentAs: z
    .object({
      actor: z.object({ login: z.string(), generation: z.number() }),
    })
    .optional(),
});
type KeptReview = z.infer<typeof keptSchema>;

const kept = createWindowDrafts<KeptReview>({ namespace: "review-submits", schema: keptSchema });

/** The entry as this window keeps it; null for nothing to keep, undefined to leave it as kept. */
function keptOf(key: string, entry: ReviewSubmitEntry): KeptReview | null | undefined {
  const parts = keyParts.get(key);
  if (!parts) return undefined;
  const { status } = entry;
  const sent =
    status.kind === "uncertain" ? status.sent : status.kind === "submitting" ? status.sent : null;
  // Discarding a pending review holds it with nothing sent; what's kept stays as it was.
  if (status.kind === "submitting" && !sent) return undefined;
  if (!sent && !entry.body.trim() && entry.event === EMPTY_REVIEW.event) return null;
  return {
    owner: parts.owner,
    prUrl: parts.prUrl,
    login: parts.login,
    ...(parts.pending ? { pending: parts.pending } : {}),
    event: entry.event,
    body: entry.body,
    ...(sent
      ? {
          held: {
            sent,
            message: status.kind === "uncertain" ? status.message : REVIEW_INTERRUPTED,
          },
        }
      : {}),
    ...(entry.sentAs ? { sentAs: entry.sentAs } : {}),
  };
}

/** A kept review as it comes back: a held one stays held, even one a reload cut off. */
function entryOf(value: KeptReview): ReviewSubmitEntry {
  return {
    event: value.event,
    body: value.body,
    status: value.held
      ? {
          kind: "uncertain",
          sent: value.held.sent,
          message: value.held.message,
          checking: false,
          checked: "never",
        }
      : { kind: "idle" },
    ...(value.sentAs ? { sentAs: value.sentAs } : {}),
  };
}

let loaded = false;

/** Reads what this window kept, once per page load, before anything reads or changes an entry. */
function ensureLoaded(): void {
  if (loaded) return;
  loaded = true;
  for (const { key, value } of kept.loadAll()) {
    keyParts.set(key, {
      owner: value.owner,
      prUrl: value.prUrl,
      login: value.login,
      ...(value.pending ? { pending: value.pending } : {}),
    });
    entries.set(key, entryOf(value));
  }
}

/** Keeps `key`'s entry as it is in memory now, and says what the write did. */
function persist(key: string): DraftWriteOutcome {
  const value = keptOf(key, entries.get(key) ?? EMPTY_REVIEW);
  return value === undefined ? "saved" : kept.save(key, value);
}

export function readReviewSubmit(key: string): ReviewSubmitEntry {
  ensureLoaded();
  return entries.get(key) ?? EMPTY_REVIEW;
}

/**
 * Changes `key`'s review and keeps it for this window, saying what that write did; a send
 * checks it before going, while typing doesn't.
 */
export function updateReviewSubmit(
  key: string,
  change: (entry: ReviewSubmitEntry) => ReviewSubmitEntry,
): DraftWriteOutcome {
  const previous = readReviewSubmit(key);
  const next = change(previous);
  if (next === previous) return "saved";
  if (next === EMPTY_REVIEW) entries.delete(key);
  else entries.set(key, next);
  changed();
  return persist(key);
}

/**
 * Removes what this window keeps for `key`, leaving memory as it is: for a send whose hold
 * couldn't be kept, so no older copy that a reload could send again is left behind.
 */
export function forgetKeptReview(key: string): DraftWriteOutcome {
  ensureLoaded();
  return kept.save(key, null);
}

/**
 * Turns a held review back into an editable draft, as the person asked once they checked
 * GitHub doesn't show it. Nothing is sent; the next submit is a new review.
 */
export function keepHeldReviewAsDraft(key: string): DraftWriteOutcome {
  return updateReviewSubmit(key, (stored) =>
    stored.status.kind === "uncertain" && !stored.status.checking
      ? { event: stored.status.sent.event, body: stored.status.sent.body, status: { kind: "idle" } }
      : stored,
  );
}

export function reviewSubmitsOf(
  owner: string,
  login: string,
): { key: string; prUrl: string; entry: ReviewSubmitEntry }[] {
  ensureLoaded();
  const wanted = login.toLowerCase();
  const found: { key: string; prUrl: string; entry: ReviewSubmitEntry }[] = [];
  for (const [key, entry] of entries) {
    const parts = keyParts.get(key);
    if (parts && parts.owner === owner && parts.login === wanted && entry.status.kind !== "idle") {
      // A pending submit is listed by its state only; what it holds stays in its own scope.
      found.push({ key, prUrl: parts.prUrl, entry: parts.pending ? masked(entry) : entry });
    }
  }
  return found;
}

/** Forget a refusal once the person has seen it, wherever it's shown. */
export function clearReviewRefusal(key: string): void {
  updateReviewSubmit(key, (stored) =>
    stored.status.kind === "refused" ? { ...stored, status: { kind: "idle" } } : stored,
  );
}

/** Re-renders on every change to any PR's review; for readers across PRs, like a list. */
export function useReviewSubmitsVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
}

export function useReviewSubmitEntry(key: string | null): ReviewSubmitEntry {
  useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
  return key ? readReviewSubmit(key) : EMPTY_REVIEW;
}

/**
 * The menu to open, and the verdict to start on, as ⌘K or a list row asks for it. A row's
 * request comes before the pull request it opens has shown its menu, so the request waits
 * for that menu a little while, and only one menu takes it.
 */
/** How long a request waits for its pull request's menu to show. */
export const REVIEW_MENU_REQUEST_WAIT_MS = CONTROL_REQUEST_WAIT_MS;

/** Ask the Review menu in `place`'s view to open on `prUrl`, on `event` where one is named. */
export function requestReviewMenu(
  prUrl: string,
  event: ReviewEvent | null,
  place: ControlPlace = { destination: null },
): void {
  requestPullRequestControl(prUrl, "review", place, event);
}

/** Changes whenever a new request is made, so a shown menu checks it. */
export const useReviewMenuRequestSerial = useControlRequestSerial;

export interface TakenReviewMenuRequest {
  event: ReviewEvent | null;
  /** Hand the request back, for a menu that goes away while it's still up. */
  release: () => void;
}

/**
 * The verdict a waiting request for `prUrl` in `place` asks the menu to open on, taking the
 * request so no other menu opens for it; undefined when none waits.
 */
export function takeReviewMenuRequest(
  prUrl: string,
  now = Date.now(),
  place: ControlPlace = { destination: null },
): TakenReviewMenuRequest | undefined {
  const request = takeControlRequest<ReviewEvent | null>(prUrl, "review", place, now);
  if (!request) return undefined;
  return {
    event: request.detail,
    // It reopens on the verdict the person has chosen since, so the request names none.
    release: () => request.release(null),
  };
}

/** A reload: memory goes, and what this window kept is read again. */
export function reloadReviewSubmitsForTests(): void {
  entries.clear();
  loaded = false;
  ensureLoaded();
  changed();
}

export function resetReviewSubmitForTests(): void {
  kept.clearForTests();
  entries.clear();
  loaded = false;
  resetControlRequestsForTests();
  changed();
}
