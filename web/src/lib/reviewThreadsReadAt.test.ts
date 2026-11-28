import { describe, expect, it } from "vitest";

import type { ReviewThread, ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";
import {
  commentPart,
  firstPageObserved,
  LISTING_PART,
  oldestReadAt,
  presentParts,
  readsAfter,
  readsComplete,
  threadPart,
  type TimedThreadsPage,
} from "@/lib/reviewThreadsReadAt";

const thread = (id: string, comments: string[]): ReviewThread =>
  ({
    id,
    comments: {
      data: comments.map((commentId) => ({ id: commentId, body: commentId })),
      has_more: false,
      next_cursor: null,
    },
  }) as unknown as ReviewThread;

const page = (threads: ReviewThread[], warning?: string): ReviewThreadsPage =>
  ({
    data: threads,
    has_more: false,
    next_cursor: null,
    head_sha: "b",
    base_sha: "a",
    ...(warning ? { warning } : {}),
  }) as unknown as ReviewThreadsPage;

const ONE = page([thread("t1", ["c1", "c2"])]);

describe("presentParts", () => {
  it("lists the listing, every thread and every comment, once", () => {
    expect([...presentParts(ONE)]).toEqual([
      LISTING_PART,
      threadPart("t1"),
      commentPart("t1", "c1"),
      commentPart("t1", "c2"),
    ]);
  });
});

describe("readsAfter", () => {
  it("dates observed parts now and keeps every other part's exact entry", () => {
    const previous = readsAfter(undefined, ONE, presentParts(ONE), 100)!;
    const older = { ...previous, [commentPart("t1", "c2")]: { at: 50 } };
    const reads = readsAfter(older, ONE, new Set([threadPart("t1")]), 200)!;
    expect(reads[threadPart("t1")]).toEqual({ at: 200 });
    expect(reads[commentPart("t1", "c2")]).toEqual({ at: 50 });
    expect(reads[LISTING_PART]).toEqual({ at: 100 });
  });

  it("drops the entries of parts no longer shown", () => {
    const previous = readsAfter(undefined, ONE, presentParts(ONE), 100)!;
    const fewer = page([thread("t1", ["c1"])]);
    expect(Object.keys(readsAfter(previous, fewer, new Set(), 200)!)).toEqual([
      LISTING_PART,
      threadPart("t1"),
      commentPart("t1", "c1"),
    ]);
  });

  it("can't tell when a part shown was read that nothing observed or dated", () => {
    const previous = readsAfter(undefined, ONE, presentParts(ONE), 100)!;
    const more = page([thread("t1", ["c1", "c2"]), thread("t2", ["c3"])]);
    expect(readsAfter(previous, more, new Set([threadPart("t2")]), 200)).toBeUndefined();
  });
});

describe("age", () => {
  const timed = (base: ReviewThreadsPage, times: Record<string, number>) =>
    ({
      ...base,
      reads: Object.fromEntries(Object.entries(times).map(([part, at]) => [part, { at }])),
    }) as TimedThreadsPage;

  it("goes by the oldest shown thread or comment, not the listing, while any show", () => {
    const shown = timed(ONE, {
      [LISTING_PART]: 10,
      [threadPart("t1")]: 300,
      [commentPart("t1", "c1")]: 300,
      [commentPart("t1", "c2")]: 200,
    });
    expect(oldestReadAt(shown)).toBe(200);
  });

  it("goes by the listing on a page that shows no conversations", () => {
    const empty = timed(page([]), { [LISTING_PART]: 10 });
    expect(oldestReadAt(empty)).toBe(10);
  });

  it("tells nothing from reads that miss a part or name one not shown", () => {
    const missing = timed(ONE, { [LISTING_PART]: 10, [threadPart("t1")]: 10 });
    expect(readsComplete(missing)).toBe(false);
    expect(oldestReadAt(missing)).toBeUndefined();
    const extra = timed(page([]), { [LISTING_PART]: 10, [threadPart("gone")]: 1 });
    expect(readsComplete(extra)).toBe(false);
  });

  it("refuses times that aren't real read times", () => {
    const bad = timed(page([]), { [LISTING_PART]: Number.NaN });
    expect(readsComplete(bad)).toBe(false);
  });
});

describe("firstPageObserved", () => {
  it("observes the listing only from a first page that didn't warn", () => {
    expect(firstPageObserved(ONE).has(LISTING_PART)).toBe(true);
    expect(firstPageObserved(page([], "partial")).has(LISTING_PART)).toBe(false);
  });
});
