// The shell's pending review read answers, as `pull_request_pending_review` sends them, for
// tests of the page's model, evidence and hook.

import type { UnplacedDraft } from "@/pullRequestReviews/pendingReview";

export const HEAD = "c".repeat(40);
export const BASE = "b".repeat(40);
export const OLDER = "d".repeat(40);
export const REVIEW_ID = "PRR_pending";

export interface DraftFixture {
  id: string;
  body?: string;
  reply_to_id?: string | null;
}

/** One draft as a placed thread holds it; `reply_to_id: undefined` as an older shell's. */
export function placedDraft(draft: DraftFixture) {
  const { id, body = "A draft." } = draft;
  const reply_to_id = "reply_to_id" in draft ? draft.reply_to_id : null;
  return {
    id,
    author: "alice",
    body,
    minimized: false,
    created_at: "2026-11-12T10:00:00Z",
    url: `https://github.com/acme/app/pull/42#discussion_${id}`,
    pending: true,
    can_update: true,
    can_delete: true,
    ...(reply_to_id === undefined ? {} : { reply_to_id }),
  };
}

/** A thread holding `drafts`, at `line` (and `start_line`) of `path`. */
export function draftThread(
  id: string,
  drafts: DraftFixture[],
  place: { path?: string; line?: number; start_line?: number | null } = {},
) {
  return {
    id,
    path: place.path ?? "src/app.ts",
    side: "RIGHT",
    line: place.line ?? 12,
    start_line: place.start_line ?? null,
    start_side: null,
    original_line: place.line ?? 12,
    original_start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "@@ -1 +1 @@",
    comments: { data: drafts.map(placedDraft), has_more: false, next_cursor: null },
  };
}

/** A draft no thread page held. */
export function unplacedDraft(
  id: string,
  fields: Partial<{ body: string; reply_to_id: string | null; path: string; line: number }> = {},
): UnplacedDraft {
  return {
    id,
    body: fields.body ?? "A draft.",
    path: fields.path ?? "src/app.ts",
    line: fields.line ?? 12,
    start_line: null,
    original_line: fields.line ?? 12,
    original_start_line: null,
    outdated: false,
    created_at: "2026-11-12T10:00:00Z",
    url: null,
    reply_to_id: fields.reply_to_id ?? null,
  };
}

/** The read's answer with no review pending. */
export function noReviewAnswer(head = HEAD) {
  return {
    object: "session.github.pr_pending_review",
    head_sha: head,
    base_sha: BASE,
    review: null,
    threads: [],
    unplaced: [],
    complete: true,
  };
}

/**
 * The read's answer with the pending review holding `threads` and `unplaced`; it counts them all
 * unless `total` says GitHub counts more.
 */
export function pendingAnswer({
  threads = [],
  unplaced = [],
  total,
  reviewId = REVIEW_ID,
  body = "",
  commit = HEAD,
  head = HEAD,
}: {
  threads?: ReturnType<typeof draftThread>[];
  unplaced?: ReturnType<typeof unplacedDraft>[];
  total?: number;
  reviewId?: string;
  body?: string;
  commit?: string | null;
  head?: string;
} = {}) {
  const read =
    threads.reduce((count, thread) => count + thread.comments.data.length, 0) + unplaced.length;
  const count = total ?? read;
  return {
    object: "session.github.pr_pending_review",
    head_sha: head,
    base_sha: BASE,
    review: { id: reviewId, body, commit_oid: commit, comment_count: count, read_count: read },
    threads,
    unplaced,
    complete: count === read,
  };
}
