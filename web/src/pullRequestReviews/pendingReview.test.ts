import { describe, expect, it } from "vitest";

import {
  BASE,
  draftThread,
  HEAD,
  noReviewAnswer,
  OLDER,
  pendingAnswer,
  REVIEW_ID,
  unplacedDraft,
} from "@/test/pendingReviewAnswers";

import type { ReviewThread, ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";

import {
  composerChoice,
  draftsNotPlaced,
  withPendingDrafts,
  listedDrafts,
  parsePendingReviewAnswer,
  type PendingReviewRead,
  pendingReviewState,
  reviewCounts,
} from "./pendingReview";

const read = (answer: unknown) => parsePendingReviewAnswer(answer) as PendingReviewRead;
const state = (fields: Partial<Parameters<typeof pendingReviewState>[0]>) =>
  pendingReviewState({
    offered: true,
    data: undefined,
    dataUpdatedAt: 0,
    failed: false,
    fetching: false,
    ...fields,
  });

describe("reading the shell's pending review answer", () => {
  it("reads no review as an authoritative none", () => {
    expect(parsePendingReviewAnswer(noReviewAnswer())).toEqual({
      head_sha: HEAD,
      base_sha: BASE,
      review: null,
      threads: [],
      unplaced: [],
      complete: true,
    });
  });

  it("reads a review's placed and unplaced drafts, with the comment each replies to", () => {
    const parsed = read(
      pendingAnswer({
        threads: [
          draftThread("PRRT_1", [
            { id: "PRRC_root" },
            { id: "PRRC_reply", reply_to_id: "PRRC_root" },
          ]),
        ],
        unplaced: [unplacedDraft("PRRC_lost", { reply_to_id: "PRRC_elsewhere" })],
      }),
    );
    expect(parsed.review).toEqual({
      id: REVIEW_ID,
      body: "",
      commit_oid: HEAD,
      comment_count: 3,
      read_count: 3,
    });
    expect(
      listedDrafts(parsed).map((draft) => [draft.id, draft.thread_id, draft.reply_to_id]),
    ).toEqual([
      ["PRRC_root", "PRRT_1", null],
      ["PRRC_reply", "PRRT_1", "PRRC_root"],
      ["PRRC_lost", null, "PRRC_elsewhere"],
    ]);
  });

  it("keeps an older shell's placed drafts, which can't say what they reply to", () => {
    const parsed = read(
      pendingAnswer({
        threads: [draftThread("PRRT_1", [{ id: "PRRC_1", reply_to_id: undefined }])],
      }),
    );
    expect(listedDrafts(parsed)[0]!.reply_to_id).toBeUndefined();
  });

  it("reads an incomplete review as incomplete, with GitHub's count", () => {
    const parsed = read(
      pendingAnswer({ threads: [draftThread("PRRT_1", [{ id: "PRRC_1" }])], total: 1001 }),
    );
    expect(parsed.complete).toBe(false);
    expect(parsed.review).toMatchObject({ comment_count: 1001, read_count: 1 });
  });

  it.each([
    ["another object", { ...noReviewAnswer(), object: "session.github.pr_review_check" }],
    [
      "an older shell's answer without unplaced drafts",
      (() => {
        const { unplaced: _unplaced, ...rest } = pendingAnswer();
        return rest;
      })(),
    ],
    [
      "a review that doesn't say how many it read",
      (() => {
        const answer = pendingAnswer();
        const { read_count: _read, ...review } = answer.review;
        return { ...answer, review };
      })(),
    ],
    ["no review with drafts", { ...noReviewAnswer(), unplaced: [unplacedDraft("PRRC_1")] }],
    ["no review called incomplete", { ...noReviewAnswer(), complete: false }],
    [
      "a draft count that doesn't match what's listed",
      { ...pendingAnswer({ unplaced: [unplacedDraft("PRRC_1")] }), unplaced: [] },
    ],
    [
      "complete with fewer read than counted",
      { ...pendingAnswer({ total: 2, unplaced: [unplacedDraft("PRRC_1")] }), complete: true },
    ],
    [
      "a draft not marked pending",
      pendingAnswer({
        threads: [
          {
            ...draftThread("PRRT_1", [{ id: "PRRC_1" }]),
            comments: {
              // An answer of another shape than the fixtures make.
              data: [
                { id: "PRRC_1", author: null, body: "", created_at: null, url: null },
              ] as never,
              has_more: false,
              next_cursor: null,
            },
          },
        ],
      }),
    ],
  ])("refuses %s", (_label, answer) => {
    expect(parsePendingReviewAnswer(answer)).toBeNull();
  });
});

describe("the page's pending review state", () => {
  it("is unavailable wherever pending reviews aren't offered, whatever was read", () => {
    expect(state({ offered: false, data: read(pendingAnswer()) })).toEqual({ kind: "unavailable" });
    expect(composerChoice({ kind: "unavailable" })).toEqual({ kind: "today" });
  });

  it("is loading until the first read answers, and sends nothing as if there were no review", () => {
    expect(state({ fetching: true })).toEqual({ kind: "loading" });
    expect(composerChoice(state({ fetching: true }))).toEqual({ kind: "checking" });
  });

  it("is an error when no read has answered, never none", () => {
    expect(state({ failed: true })).toEqual({ kind: "error" });
    expect(composerChoice({ kind: "error" })).toEqual({ kind: "unreadable" });
    // A retry in flight is loading again.
    expect(state({ failed: true, fetching: true })).toEqual({ kind: "loading" });
  });

  it("offers Comment or Start a review after a read that found none", () => {
    const none = state({ data: read(noReviewAnswer()), dataUpdatedAt: 5 });
    expect(none).toMatchObject({ kind: "none", stale: false, checkedAt: 5 });
    expect(composerChoice(none)).toEqual({ kind: "comment_or_start" });
  });

  it("offers only Add to review while a review is pending, complete or not", () => {
    const complete = state({ data: read(pendingAnswer({ unplaced: [unplacedDraft("PRRC_1")] })) });
    expect(complete).toMatchObject({ kind: "pending", complete: true, outdated: null });
    expect(composerChoice(complete)).toEqual({
      kind: "add_to_review",
      reviewId: REVIEW_ID,
      outdated: null,
    });
    const incomplete = state({
      data: read(pendingAnswer({ unplaced: [unplacedDraft("PRRC_1")], total: 40 })),
    });
    expect(incomplete).toMatchObject({ kind: "pending", complete: false });
    expect(composerChoice(incomplete)).toMatchObject({ kind: "add_to_review" });
    // Finish and Discard say all of GitHub's count, never the list's length.
    expect(reviewCounts(incomplete)).toEqual({ total: 40, shown: 1 });
  });

  it("keeps the last good read, marked stale, when a refetch fails", () => {
    const none = state({ data: read(noReviewAnswer()), failed: true, dataUpdatedAt: 9 });
    expect(none).toMatchObject({ kind: "none", stale: true, checkedAt: 9 });
    const pending = state({ data: read(pendingAnswer()), failed: true });
    expect(pending).toMatchObject({ kind: "pending", stale: true });
  });

  it("says when the review is on an older commit than the head shown", () => {
    const outdated = state({ data: read(pendingAnswer({ commit: OLDER })) });
    expect(outdated).toMatchObject({ outdated: { reviewCommit: OLDER, head: HEAD } });
    expect(composerChoice(outdated)).toMatchObject({
      kind: "add_to_review",
      outdated: { reviewCommit: OLDER, head: HEAD },
    });
    expect(state({ data: read(pendingAnswer({ commit: null })) })).toMatchObject({
      outdated: null,
    });
  });
});

describe("drafts shown in their threads", () => {
  const published = (overrides: Partial<ReviewThreadsPage> = {}): ReviewThreadsPage => ({
    data: [
      {
        ...draftThread("PRRT_1", [{ id: "PRRC_root" }]),
        comments: {
          data: [
            {
              id: "PRRC_root",
              author: "carol",
              body: "Published.",
              created_at: "2026-11-12T09:00:00Z",
              url: null,
            },
          ],
          has_more: false,
          next_cursor: null,
        },
      } as ReviewThread,
    ],
    has_more: false,
    next_cursor: null,
    head_sha: HEAD,
    base_sha: BASE,
    ...overrides,
  });
  const pending = (answer: unknown) => state({ data: read(answer) });

  it("puts a reply after the thread's published comments, marked pending with its review", () => {
    const shown = withPendingDrafts(
      published(),
      pending(pendingAnswer({ threads: [draftThread("PRRT_1", [{ id: "PRRC_reply" }])] })),
    )!;
    expect(shown.data).toHaveLength(1);
    expect(
      shown.data[0]!.comments.data.map((comment) => [
        comment.id,
        comment.pending ?? false,
        comment.pending_review_id,
      ]),
    ).toEqual([
      ["PRRC_root", false, undefined],
      ["PRRC_reply", true, REVIEW_ID],
    ]);
    expect(shown.data[0]!.can_reply).toBe(true);
  });

  it("adds a thread only the viewer can see yet, which takes no reply here", () => {
    const shown = withPendingDrafts(
      published(),
      pending(pendingAnswer({ threads: [draftThread("PRRT_new", [{ id: "PRRC_new" }])] })),
    )!;
    expect(shown.data.map((thread) => [thread.id, thread.can_reply])).toEqual([
      ["PRRT_1", true],
      ["PRRT_new", false],
    ]);
  });

  it("leaves the published page as it is without a pending review", () => {
    const page = published();
    expect(withPendingDrafts(page, state({ data: read(noReviewAnswer()) }))).toBe(page);
    expect(withPendingDrafts(page, { kind: "unavailable" })).toBe(page);
    expect(withPendingDrafts(page, { kind: "error" })).toBe(page);
    expect(withPendingDrafts(undefined, pending(pendingAnswer()))).toBeUndefined();
  });

  it("places nothing on a page of another head, and lists those drafts as not shown", () => {
    const answer = pendingAnswer({
      threads: [draftThread("PRRT_1", [{ id: "PRRC_reply" }])],
      unplaced: [unplacedDraft("PRRC_lost")],
    });
    const page = published({ head_sha: OLDER });
    expect(withPendingDrafts(page, pending(answer))).toBe(page);
    expect(draftsNotPlaced(page, pending(answer)).map((draft) => draft.id)).toEqual([
      "PRRC_reply",
      "PRRC_lost",
    ]);
    // On the same head, only the drafts no thread page held.
    expect(draftsNotPlaced(published(), pending(answer)).map((draft) => draft.id)).toEqual([
      "PRRC_lost",
    ]);
    expect(draftsNotPlaced(published(), { kind: "error" })).toEqual([]);
  });
});
