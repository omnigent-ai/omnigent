// The account's pending review on a pull request, as this Mac reads it: its id, summary, commit
// and drafts, and what the page may do with it. Drafts are only their author's until the review
// is submitted; a read that couldn't list them all never stands in for all of them.

import {
  mergeReviewComments,
  type ReviewComment,
  type ReviewThread,
  type ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";

/** The badge a draft carries, and what it means. */
export const PENDING_LABEL = "Pending";
export const PENDING_DESCRIPTION = "Only you can see this until you submit your review.";

/** A draft as a thread shows it, naming the comment it replies to, or null when it starts it. */
export interface PendingDraft extends ReviewComment {
  pending: true;
  /** Absent from an older shell's read, which can't say. */
  reply_to_id?: string | null;
}

/** A thread with only the review's drafts in it, at the thread's own place in the diff. */
export interface PendingDraftThread extends Omit<ReviewThread, "comments"> {
  comments: { data: PendingDraft[]; has_more: boolean; next_cursor: string | null };
}

/** A draft no thread page held: listed at its own lines, with no side and nowhere to reply. */
export interface UnplacedDraft {
  id: string;
  body: string;
  path: string | null;
  line: number | null;
  start_line: number | null;
  original_line: number | null;
  original_start_line: number | null;
  outdated: boolean;
  created_at: string | null;
  url: string | null;
  reply_to_id: string | null;
}

export interface PendingReview {
  id: string;
  body: string;
  /** The commit the review was started on; it's submitted on that commit, whatever the head. */
  commit_oid: string | null;
  /** How many drafts GitHub counts in it. */
  comment_count: number;
  /** How many of them this read listed. */
  read_count: number;
}

export interface PendingReviewRead {
  head_sha: string;
  base_sha: string | null;
  review: PendingReview | null;
  threads: PendingDraftThread[];
  unplaced: UnplacedDraft[];
  /** True only when every draft GitHub counts was read. */
  complete: boolean;
  /** When this read was sent, by this Mac's clock; only a read sent after a change says how it ended. */
  issued_at?: number;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === "string";
const isTextOrNull = (value: unknown): value is string | null => value === null || isText(value);
const isLineOrNull = (value: unknown): value is number | null =>
  value === null || (Number.isInteger(value) && (value as number) > 0);
const isCount = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const isSide = (value: unknown) => value === "LEFT" || value === "RIGHT";
const optional = (value: unknown, check: (value: unknown) => boolean) =>
  value === undefined || check(value);

function isPendingDraft(value: unknown): value is PendingDraft {
  return (
    isObject(value) &&
    isText(value.id) &&
    value.id.length > 0 &&
    isTextOrNull(value.author) &&
    isText(value.body) &&
    isTextOrNull(value.created_at) &&
    isTextOrNull(value.url) &&
    value.pending === true &&
    optional(value.reply_to_id, isTextOrNull) &&
    optional(value.can_update, (flag) => typeof flag === "boolean") &&
    optional(value.can_delete, (flag) => typeof flag === "boolean")
  );
}

function isDraftThread(value: unknown): value is PendingDraftThread {
  if (!isObject(value) || !isText(value.id) || value.id.length === 0) return false;
  const comments = value.comments;
  return (
    isText(value.path) &&
    isSide(value.side) &&
    isLineOrNull(value.line) &&
    isLineOrNull(value.start_line) &&
    optional(value.start_side, (side) => side === null || isSide(side)) &&
    optional(value.original_line, isLineOrNull) &&
    optional(value.original_start_line, isLineOrNull) &&
    (value.subject_type === "LINE" || value.subject_type === "FILE") &&
    typeof value.resolved === "boolean" &&
    typeof value.outdated === "boolean" &&
    typeof value.can_reply === "boolean" &&
    isText(value.diff_hunk) &&
    isObject(comments) &&
    Array.isArray(comments.data) &&
    comments.data.length > 0 &&
    comments.data.every(isPendingDraft)
  );
}

function isUnplacedDraft(value: unknown): value is UnplacedDraft {
  return (
    isObject(value) &&
    isText(value.id) &&
    value.id.length > 0 &&
    isText(value.body) &&
    isTextOrNull(value.path) &&
    isLineOrNull(value.line) &&
    isLineOrNull(value.start_line) &&
    isLineOrNull(value.original_line) &&
    isLineOrNull(value.original_start_line) &&
    typeof value.outdated === "boolean" &&
    isTextOrNull(value.created_at) &&
    isTextOrNull(value.url) &&
    isTextOrNull(value.reply_to_id)
  );
}

function parseReview(value: unknown): PendingReview | null {
  if (
    !isObject(value) ||
    !isText(value.id) ||
    value.id.length === 0 ||
    !isText(value.body) ||
    !isTextOrNull(value.commit_oid) ||
    !isCount(value.comment_count) ||
    !isCount(value.read_count)
  ) {
    return null;
  }
  return {
    id: value.id,
    body: value.body,
    commit_oid: value.commit_oid,
    comment_count: value.comment_count,
    read_count: value.read_count,
  };
}

/**
 * The pending review read's answer, or null for anything else, including an older shell's
 * answer that doesn't say how many drafts it read: the page then treats the read as failed.
 */
export function parsePendingReviewAnswer(data: unknown): PendingReviewRead | null {
  if (!isObject(data) || data.object !== "session.github.pr_pending_review") return null;
  if (!isText(data.head_sha) || !isTextOrNull(data.base_sha)) return null;
  if (typeof data.complete !== "boolean") return null;
  if (!Array.isArray(data.threads) || !data.threads.every(isDraftThread)) return null;
  if (!Array.isArray(data.unplaced) || !data.unplaced.every(isUnplacedDraft)) return null;
  const base = { head_sha: data.head_sha, base_sha: data.base_sha };
  if (data.review === null) {
    // No review has no drafts; anything else is another answer than this one.
    if (data.threads.length > 0 || data.unplaced.length > 0 || !data.complete) return null;
    return { ...base, review: null, threads: [], unplaced: [], complete: true };
  }
  const review = parseReview(data.review);
  if (!review) return null;
  const listed = data.threads.reduce((count, thread) => count + thread.comments.data.length, 0);
  // Every draft shown was read, and what was read is all GitHub counts only when complete.
  if (listed + data.unplaced.length !== review.read_count) return null;
  if (review.read_count > review.comment_count) return null;
  if (data.complete && review.read_count !== review.comment_count) return null;
  return {
    ...base,
    review,
    threads: data.threads,
    unplaced: data.unplaced,
    complete: data.complete,
  };
}

/** Where a new draft goes: lines of the diff the page shows, or a reply in a thread. */
export type DraftPlace =
  | {
      kind: "lines";
      path: string;
      side: "LEFT" | "RIGHT";
      line: number;
      startLine: number | null;
      headSha: string;
      baseSha: string;
    }
  | { kind: "reply"; threadId: string };

/** One of the viewer's reviews read by its id: its state, `absent`, or `unreadable`. */
export type ReviewNodeState =
  | "absent"
  | "unreadable"
  | "PENDING"
  | "COMMENTED"
  | "APPROVED"
  | "CHANGES_REQUESTED"
  | "DISMISSED";

const REVIEW_NODE_STATES: ReadonlySet<string> = new Set([
  "absent",
  "unreadable",
  "PENDING",
  "COMMENTED",
  "APPROVED",
  "CHANGES_REQUESTED",
  "DISMISSED",
]);

/** The review state read's answer for `reviewId`, or null for anything else. */
export function parseReviewStateAnswer(data: unknown, reviewId: string): ReviewNodeState | null {
  return isObject(data) &&
    data.object === "session.github.pr_review_state" &&
    data.review_id === reviewId &&
    isText(data.state) &&
    REVIEW_NODE_STATES.has(data.state)
    ? (data.state as ReviewNodeState)
    : null;
}

/**
 * The local draft a pending add was sent from: its draft key, the id the send stamped on it
 * (dropped by any later edit), the body sent, and its place. Only that exact draft clears.
 */
export interface SentDraft {
  draftKey: readonly string[];
  sentId: string;
  body: string;
  place: DraftPlace;
  /** The half-done start this add completes, which a confirmed add retires. */
  recoveryId?: string;
}

const sameLines = (left: DraftPlace, right: DraftPlace) =>
  left.kind === "lines" &&
  right.kind === "lines" &&
  left.path === right.path &&
  left.side === right.side &&
  left.line === right.line &&
  left.startLine === right.startLine &&
  left.headSha === right.headSha &&
  left.baseSha === right.baseSha;

/** A place as one string, for keys: a thread's id, or every coordinate of its lines. */
export function placeKey(place: DraftPlace): string {
  return place.kind === "reply"
    ? `thread:${place.threadId}`
    : [
        "lines",
        place.path,
        place.side,
        place.startLine ?? "",
        place.line,
        place.headSha,
        place.baseSha,
      ].join("\n");
}

/** Whether two places are the same: one thread, or the same lines, side, head and base. */
export function samePlace(left: DraftPlace, right: DraftPlace): boolean {
  if (left.kind === "reply" || right.kind === "reply") {
    return left.kind === "reply" && right.kind === "reply" && left.threadId === right.threadId;
  }
  return sameLines(left, right);
}

/** Every draft the read listed, placed or not, with the lines it's on. */
export interface ListedDraft {
  id: string;
  body: string;
  /** Undefined where an older shell's read didn't say. */
  reply_to_id: string | null | undefined;
  thread_id: string | null;
  path: string | null;
  line: number | null;
  start_line: number | null;
  /** The side of the diff its lines are on; null for a draft no thread placed, which can't say. */
  side: "LEFT" | "RIGHT" | null;
  start_side: "LEFT" | "RIGHT" | null;
}

export function listedDrafts(read: PendingReviewRead): ListedDraft[] {
  const placed = read.threads.flatMap((thread) =>
    thread.comments.data.map((draft) => ({
      id: draft.id,
      body: draft.body,
      reply_to_id: draft.reply_to_id,
      thread_id: thread.id,
      path: thread.path,
      line: thread.line,
      start_line: thread.start_line,
      side: thread.side,
      start_side: thread.start_side ?? null,
    })),
  );
  const unplaced = read.unplaced.map((draft) => ({
    id: draft.id,
    body: draft.body,
    reply_to_id: draft.reply_to_id,
    thread_id: null,
    path: draft.path,
    line: draft.line,
    start_line: draft.start_line,
    side: null,
    start_side: null,
  }));
  return [...placed, ...unplaced];
}

/** A review started on an older commit than the head the page shows. */
export interface OutdatedReview {
  reviewCommit: string;
  head: string;
}

/** Where the review is pinned behind the head, or null when it's on the head or can't say. */
export function outdatedReview(read: PendingReviewRead): OutdatedReview | null {
  const commit = read.review?.commit_oid;
  return commit && commit !== read.head_sha ? { reviewCommit: commit, head: read.head_sha } : null;
}

/**
 * What the page knows about the account's pending review:
 * - `unavailable`: not offered here (the shell, route, flag or account), so today's behavior;
 * - `loading`: the first read is in flight;
 * - `error`: no read has answered; nothing may be sent as if there were no review;
 * - `none` and `pending`: the last good read, `stale` when a later one failed.
 */
export type PendingReviewState =
  | { kind: "unavailable" }
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "none"; read: PendingReviewRead; stale: boolean; checkedAt: number }
  | {
      kind: "pending";
      read: PendingReviewRead;
      review: PendingReview;
      complete: boolean;
      outdated: OutdatedReview | null;
      stale: boolean;
      checkedAt: number;
    };

/** The state from whether it's offered and what its query holds. */
export function pendingReviewState(query: {
  offered: boolean;
  data: PendingReviewRead | undefined;
  /** When `data` was read, in milliseconds. */
  dataUpdatedAt: number;
  failed: boolean;
  fetching: boolean;
}): PendingReviewState {
  if (!query.offered) return { kind: "unavailable" };
  const { data } = query;
  if (!data) return query.failed && !query.fetching ? { kind: "error" } : { kind: "loading" };
  // A failed refetch keeps the last good read, marked stale; it never turns into `none`.
  const stale = query.failed;
  const checkedAt = query.dataUpdatedAt;
  if (!data.review) return { kind: "none", read: data, stale, checkedAt };
  return {
    kind: "pending",
    read: data,
    review: data.review,
    complete: data.complete,
    outdated: outdatedReview(data),
    stale,
    checkedAt,
  };
}

/**
 * What a new comment or reply may offer:
 * - `today`: pending reviews aren't offered, so today's immediate comment;
 * - `checking` and `unreadable`: nothing is sent until a read says whether a review is pending;
 * - `comment_or_start`: Comment now, or Start a review;
 * - `add_to_review`: only Add to review, into the pending review.
 */
export type ComposerChoice =
  | { kind: "today" }
  | { kind: "checking" }
  | { kind: "unreadable" }
  | { kind: "comment_or_start" }
  | { kind: "add_to_review"; reviewId: string; outdated: OutdatedReview | null };

export function composerChoice(state: PendingReviewState): ComposerChoice {
  switch (state.kind) {
    case "unavailable":
      return { kind: "today" };
    case "loading":
      return { kind: "checking" };
    case "error":
      return { kind: "unreadable" };
    case "none":
      return { kind: "comment_or_start" };
    case "pending":
      return { kind: "add_to_review", reviewId: state.review.id, outdated: state.outdated };
  }
}

/**
 * What finishing or discarding may say about the review's size: all its drafts, as GitHub
 * counts them, and how many are shown. Never the partial list's length.
 */
export function reviewCounts(state: PendingReviewState): { total: number; shown: number } | null {
  if (state.kind !== "pending") return null;
  return { total: state.review.comment_count, shown: state.review.read_count };
}

/**
 * `page` with the pending review's drafts where they'll appear, for display only: a reply in its
 * thread after the published comments, and a new conversation as a thread of its own. Nothing
 * is placed on a page of another head or base, whose lines may be other lines. A thread only the
 * viewer can see yet takes no reply here: a reply to it goes into the review.
 */
export function withPendingDrafts<Page extends ReviewThreadsPage>(
  page: Page | undefined,
  state: PendingReviewState,
): Page | undefined {
  if (!page || state.kind !== "pending" || state.read.threads.length === 0) return page;
  const { read, review } = state;
  if (read.head_sha !== page.head_sha || (read.base_sha ?? page.base_sha) !== page.base_sha) {
    return page;
  }
  const drafts = new Map(
    read.threads.map((thread) => [
      thread.id,
      thread.comments.data.map((draft) => ({
        ...draft,
        pending: true,
        pending_review_id: review.id,
      })),
    ]),
  );
  const data = page.data.map((thread) => {
    const own = drafts.get(thread.id);
    if (!own) return thread;
    drafts.delete(thread.id);
    const published = thread.comments.data.filter((comment) => !comment.pending);
    // Published already, as after a submit elsewhere: the published comment is what's there now.
    const publishedIds = new Set(published.map((comment) => comment.id));
    const stillDrafts = own.filter((draft) => !publishedIds.has(draft.id));
    return {
      ...thread,
      comments: { ...thread.comments, data: mergeReviewComments(published, stillDrafts) },
    };
  });
  const pendingOnly: ReviewThread[] = read.threads
    .filter((thread) => drafts.has(thread.id))
    .map((thread) => ({
      ...thread,
      can_reply: false,
      comments: { ...thread.comments, data: drafts.get(thread.id)! },
    }));
  return { ...page, data: [...data, ...pendingOnly] };
}

/** The read's drafts that no shown thread holds: listed, with a link, and nowhere to reply. */
export function draftsNotPlaced(
  page: ReviewThreadsPage | undefined,
  state: PendingReviewState,
): UnplacedDraft[] {
  if (state.kind !== "pending") return [];
  const { read } = state;
  const placedOnPage =
    !!page && read.head_sha === page.head_sha && (read.base_sha ?? page.base_sha) === page.base_sha;
  if (placedOnPage) return read.unplaced;
  // On another head than the threads shown, none of its drafts are placed yet.
  return [
    ...read.threads.flatMap((thread) =>
      thread.comments.data.map((draft) => ({
        id: draft.id,
        body: draft.body,
        path: thread.path,
        line: thread.line,
        start_line: thread.start_line,
        original_line: thread.original_line ?? null,
        original_start_line: thread.original_start_line ?? null,
        outdated: thread.outdated,
        created_at: draft.created_at,
        url: draft.url,
        reply_to_id: draft.reply_to_id ?? null,
      })),
    ),
    ...read.unplaced,
  ];
}
