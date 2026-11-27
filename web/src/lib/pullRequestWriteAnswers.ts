// The answers the session's host gives to pull request writes, parsed before they update
// the page, and the error a write raises when it doesn't finish. Host-neutral.

import type {
  CodeHostWriteSteps,
  PendingDraftBaseline,
  PendingReviewStart,
} from "@/lib/nativeBridge";

/** What a failed several-step or pending review write reported beyond its code. */
export interface PullRequestWriteErrorDetails {
  /** Why a `partial` run stopped. */
  cause?: string;
  /** A batch refused before its sheet: each of its pull requests, and why. */
  results?: unknown;
  steps?: CodeHostWriteSteps;
  draftBaseline?: PendingDraftBaseline;
  reviewStart?: PendingReviewStart;
}

/** A pull request write that didn't finish; `outcomeUnknown` means GitHub may have made it. */
export class PullRequestWriteError extends Error {
  readonly code: string;
  /** For a title or description that changed on GitHub: what it is there now. */
  readonly current?: { field: "title" | "body"; value: string };
  /** For a review GitHub didn't confirm: the viewer's reviews that were there before it. */
  readonly knownReviewIds?: readonly string[];
  readonly details: PullRequestWriteErrorDetails;
  constructor(
    code: string,
    message: string,
    current?: { field: "title" | "body"; value: string },
    knownReviewIds?: readonly string[],
    details: PullRequestWriteErrorDetails = {},
  ) {
    super(message);
    this.name = "PullRequestWriteError";
    this.code = code;
    this.current = current;
    this.knownReviewIds = knownReviewIds;
    this.details = details;
  }
  get outcomeUnknown(): boolean {
    return this.code === "outcome_unknown";
  }
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === "string";
const isTextOrNull = (value: unknown) => value === null || isText(value);
const isLineOrNull = (value: unknown) =>
  value === null || (Number.isInteger(value) && (value as number) > 0);
const optional = (value: unknown, check: (value: unknown) => boolean) =>
  value === undefined || check(value);
const isSide = (value: unknown) => value === "LEFT" || value === "RIGHT";

/** A conversation comment as the panel shows it: `{ author, body, created_at, url }`. */
function isPostedComment(value: unknown): boolean {
  return (
    isObject(value) &&
    isTextOrNull(value.author) &&
    optional(value.author_id, isTextOrNull) &&
    isText(value.body) &&
    isTextOrNull(value.created_at) &&
    // A posted comment always has its link; the panel merges by it.
    isText(value.url)
  );
}

/** A review comment as the panel shows it, with the id a reply is tracked by. */
function isReviewComment(value: unknown): boolean {
  return (
    isObject(value) &&
    isText(value.id) &&
    value.id.length > 0 &&
    isTextOrNull(value.author) &&
    isText(value.body) &&
    isTextOrNull(value.created_at) &&
    isTextOrNull(value.url) &&
    optional(value.minimized, (minimized) => typeof minimized === "boolean")
  );
}

/** A review conversation with every field the panel reads, and every comment in it. */
function isReviewThread(value: unknown): boolean {
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
    value.pending_comment_ids === undefined &&
    isObject(comments) &&
    Array.isArray(comments.data) &&
    comments.data.length > 0 &&
    comments.data.every(isReviewComment) &&
    typeof comments.has_more === "boolean" &&
    isTextOrNull(comments.next_cursor) &&
    optional(comments.warning, isText)
  );
}

/** A comment answer, `{ comment }` shaped as the panel shows it, or null. */
export function parseCommentAnswer<Comment>(data: unknown): { comment: Comment } | null {
  return isObject(data) && isPostedComment(data.comment)
    ? { comment: data.comment as Comment }
    : null;
}

/** A reply answer, `{ comment }` shaped as a review comment with its id, or null. */
export function parseReplyAnswer<Comment>(data: unknown): { comment: Comment } | null {
  return isObject(data) && isReviewComment(data.comment)
    ? { comment: data.comment as Comment }
    : null;
}

/** A new conversation's answer, `{ thread }`; null `thread` means main couldn't read it back. */
export function parseThreadAnswer<Thread>(data: unknown): { thread: Thread | null } | null {
  if (!isObject(data) || !Object.hasOwn(data, "thread")) return null;
  if (data.thread === null) return { thread: null };
  return isReviewThread(data.thread) ? { thread: data.thread as Thread } : null;
}

const isLabel = (value: unknown) =>
  isObject(value) &&
  isText(value.name) &&
  value.name.length > 0 &&
  isTextOrNull(value.color) &&
  isTextOrNull(value.description);

/** A label change's answer: the labels now, or null when main couldn't read them back. */
export function parseLabelsAnswer<Label>(data: unknown): { labels: Label[] | null } | null {
  if (!isObject(data) || !Object.hasOwn(data, "labels")) return null;
  if (data.labels === null) return { labels: null };
  return Array.isArray(data.labels) && data.labels.every(isLabel)
    ? { labels: data.labels as Label[] }
    : null;
}

const REVIEWER_STATES = new Set([
  "PENDING",
  "APPROVED",
  "COMMENTED",
  "CHANGES_REQUESTED",
  "DISMISSED",
]);
const isReviewer = (value: unknown) =>
  isObject(value) &&
  isText(value.login) &&
  value.login.length > 0 &&
  (value.kind === "user" || value.kind === "team") &&
  isText(value.state) &&
  REVIEWER_STATES.has(value.state);

/** A reviewer change's answer: the reviewers now, or null when main couldn't read them back. */
export function parseReviewersAnswer<Reviewer>(
  data: unknown,
): { reviewers: Reviewer[] | null } | null {
  if (!isObject(data) || !Object.hasOwn(data, "reviewers")) return null;
  if (data.reviewers === null) return { reviewers: null };
  return Array.isArray(data.reviewers) && data.reviewers.every(isReviewer)
    ? { reviewers: data.reviewers as Reviewer[] }
    : null;
}

/** A merge or auto-merge change's answer: the PR it was made to. */
export function parseMergeAnswer(data: unknown): { pr_url: string } | null {
  return isObject(data) && data.object === "session.github.pr_merge" && isText(data.pr_url)
    ? { pr_url: data.pr_url }
    : null;
}

/** A merge queue change's answer: whether the PR is in the queue now, as main made it. */
export function parseMergeQueueAnswer(data: unknown): { pr_url: string; queued: boolean } | null {
  return isObject(data) &&
    data.object === "session.github.pr_merge_queue" &&
    isText(data.pr_url) &&
    typeof data.queued === "boolean"
    ? { pr_url: data.pr_url, queued: data.queued }
    : null;
}

/** The state a close or reopen left the pull request in, as GitHub answered it. */
export type PullRequestState = "OPEN" | "CLOSED" | "MERGED";

/** A close or reopen's answer: the PR and the state GitHub reported, never the one asked for. */
export function parsePullRequestStateAnswer(
  data: unknown,
): { pr_url: string; state: PullRequestState } | null {
  return isObject(data) &&
    data.object === "session.github.pr_state" &&
    isText(data.pr_url) &&
    (data.state === "OPEN" || data.state === "CLOSED" || data.state === "MERGED")
    ? { pr_url: data.pr_url, state: data.state }
    : null;
}

/** A draft or ready change's answer: whether GitHub has the PR as a draft now. */
export function parsePullRequestDraftAnswer(
  data: unknown,
): { pr_url: string; is_draft: boolean } | null {
  return isObject(data) &&
    data.object === "session.github.pr_draft" &&
    isText(data.pr_url) &&
    typeof data.is_draft === "boolean"
    ? { pr_url: data.pr_url, is_draft: data.is_draft }
    : null;
}

/** Turning on auto-merge's answer: when GitHub says it was turned on. */
export function parseAutoMergeEnableAnswer(
  data: unknown,
): { pr_url: string; enabled_at: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_auto_merge" &&
    isText(data.pr_url) &&
    isText(data.enabled_at)
    ? { pr_url: data.pr_url, enabled_at: data.enabled_at }
    : null;
}

/** How one pull request of a batch ended, as main's ledger says. */
export const AUTO_MERGE_BATCH_OUTCOMES = [
  "enabled",
  "refused",
  "unknown",
  "not_attempted",
  "excluded",
] as const;
export type AutoMergeBatchOutcome = (typeof AUTO_MERGE_BATCH_OUTCOMES)[number];

/** One pull request's result in a batch that turned auto-merge on. */
export interface AutoMergeBatchResult {
  /** Its host, repository and number: its identity in the batch. */
  host: string;
  repository: string;
  number: number;
  outcome: AutoMergeBatchOutcome;
  /** Why it was refused or left out. */
  code: string | null;
  /** Why it wasn't attempted: what stopped the batch. */
  cause: string | null;
  /** main's own words for a refusal. */
  message: string | null;
}

/** Each result a batch's answer lists, or null where any is malformed. */
export function parseAutoMergeBatchResults(value: unknown): AutoMergeBatchResult[] | null {
  if (!Array.isArray(value)) return null;
  const results: AutoMergeBatchResult[] = [];
  for (const entry of value) {
    if (
      !isObject(entry) ||
      !isText(entry.host) ||
      !isText(entry.repository) ||
      typeof entry.number !== "number" ||
      !AUTO_MERGE_BATCH_OUTCOMES.includes(entry.outcome as AutoMergeBatchOutcome)
    ) {
      return null;
    }
    results.push({
      host: entry.host,
      repository: entry.repository,
      number: entry.number,
      outcome: entry.outcome as AutoMergeBatchOutcome,
      code: isText(entry.code) ? entry.code : null,
      cause: isText(entry.cause) ? entry.cause : null,
      message: isText(entry.message) ? entry.message : null,
    });
  }
  return results;
}

/** Turning on auto-merge for several pull requests: each one's result. */
export function parseAutoMergeManyAnswer(
  data: unknown,
): { results: AutoMergeBatchResult[] } | null {
  if (!isObject(data) || data.object !== "session.github.pr_auto_merge_many") return null;
  const results = parseAutoMergeBatchResults(data.results);
  return results ? { results } : null;
}

/** A batch member's identity: its host, repository and number, ignoring case. */
export function autoMergeBatchKey(
  member: Pick<AutoMergeBatchResult, "host" | "repository" | "number">,
): string {
  return [member.host.toLowerCase(), member.repository.toLowerCase(), member.number].join("\n");
}

/** A comment edit's answer: the comment's text as GitHub saved it. */
export function parseCommentEditAnswer(data: unknown): { comment_id: string; body: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_comment_edit" &&
    isText(data.comment_id) &&
    isText(data.body)
    ? { comment_id: data.comment_id, body: data.body }
    : null;
}

/** A comment delete's answer, naming the comment GitHub deleted. */
export function parseCommentDeleteAnswer(data: unknown): { comment_id: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_comment_delete" &&
    isText(data.comment_id)
    ? { comment_id: data.comment_id }
    : null;
}

/** A resolution's answer: the conversation GitHub changed, and whether it's now resolved. */
export function parseThreadResolutionAnswer(
  data: unknown,
): { thread_id: string; resolved: boolean } | null {
  return isObject(data) &&
    data.object === "session.github.pr_review_thread_resolution" &&
    isText(data.thread_id) &&
    typeof data.resolved === "boolean"
    ? { thread_id: data.thread_id, resolved: data.resolved }
    : null;
}

/** A draft added to a pending review: a new conversation, or a reply in one. */
export type DraftAddAnswer<Thread, Comment> =
  | { review_id: string; thread: Thread }
  | { review_id: string; thread_id: string; comment: Comment };

/** A draft add's answer: the review it's in, and the thread or reply GitHub made, or null. */
export function parseDraftAddAnswer<Thread, Comment>(
  data: unknown,
): DraftAddAnswer<Thread, Comment> | null {
  if (!isObject(data) || data.object !== "session.github.pr_review_draft") return null;
  if (!isText(data.review_id) || data.review_id.length === 0) return null;
  if (data.thread !== undefined) {
    return isReviewThread(data.thread)
      ? { review_id: data.review_id, thread: data.thread as Thread }
      : null;
  }
  return isText(data.thread_id) && isReviewComment(data.comment)
    ? { review_id: data.review_id, thread_id: data.thread_id, comment: data.comment as Comment }
    : null;
}

/** A discard's answer: the pending review GitHub deleted, or null. */
export function parseReviewDiscardAnswer(data: unknown): { review_id: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_review_discard" &&
    isText(data.review_id) &&
    data.review_id.length > 0
    ? { review_id: data.review_id }
    : null;
}

/** What Check Again read of one comment: there (with its text), gone, or not knowable. */
export type CommentState =
  { state: "present"; body: string } | { state: "absent" } | { state: "unreadable" };

export function parseCommentStateAnswer(data: unknown, commentId: string): CommentState | null {
  if (!isObject(data) || data.object !== "session.github.pr_comment_state") return null;
  if (data.comment_id !== commentId) return null;
  if (data.state === "present")
    return isText(data.body) ? { state: "present", body: data.body } : null;
  if (data.state === "absent" || data.state === "unreadable") return { state: data.state };
  return null;
}

/** A submitted review, as GitHub answered it: never the state asked for. */
export interface SubmittedReview {
  id: string;
  state: "COMMENTED" | "APPROVED" | "CHANGES_REQUESTED";
  body: string;
  created_at: string | null;
  url: string | null;
  commit_oid: string;
}

const SUBMITTED_STATES = new Set(["COMMENTED", "APPROVED", "CHANGES_REQUESTED"]);

/** A review submit's answer: the PR, and the review GitHub made. */
export function parseReviewSubmitAnswer(
  data: unknown,
): { pr_url: string; review: SubmittedReview } | null {
  if (!isObject(data) || data.object !== "session.github.pr_review" || !isText(data.pr_url)) {
    return null;
  }
  const review = data.review;
  return isObject(review) &&
    isText(review.id) &&
    SUBMITTED_STATES.has(review.state as string) &&
    isText(review.body) &&
    isTextOrNull(review.created_at) &&
    isTextOrNull(review.url) &&
    isText(review.commit_oid)
    ? { pr_url: data.pr_url, review: review as unknown as SubmittedReview }
    : null;
}

/** One of the viewer's submitted reviews, as the review check reads it. */
export interface CheckedReview {
  id: string;
  state: string;
  body: string;
  created_at: string | null;
  commit_oid: string | null;
}

/** What the review check read: the head, whether a review is pending, the viewer's reviews. */
export interface ReviewCheck {
  head_sha: string;
  author: string | null;
  pending: boolean;
  reviews: CheckedReview[];
}

export function parseReviewCheckAnswer(data: unknown): ReviewCheck | null {
  if (!isObject(data) || data.object !== "session.github.pr_review_check") return null;
  if (!isText(data.head_sha) || !isTextOrNull(data.author) || typeof data.pending !== "boolean") {
    return null;
  }
  if (!Array.isArray(data.reviews)) return null;
  const reviews = data.reviews.filter(
    (review): review is CheckedReview =>
      isObject(review) &&
      isText(review.id) &&
      isText(review.state) &&
      isText(review.body) &&
      isTextOrNull(review.created_at) &&
      isTextOrNull(review.commit_oid),
  );
  if (reviews.length !== data.reviews.length) return null;
  return { head_sha: data.head_sha, author: data.author, pending: data.pending, reviews };
}

/** A title or description change's answer: both as GitHub has them now. */
export function parseDetailsAnswer(data: unknown): { title: string; body: string } | null {
  return isObject(data) && isText(data.title) && isText(data.body)
    ? { title: data.title, body: data.body }
    : null;
}
