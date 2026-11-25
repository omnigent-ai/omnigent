import { describe, expect, it } from "vitest";

import type { ReviewThread, ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";
import {
  commentPart,
  firstPageObserved,
  LISTING_PART,
  oldestReadAt,
  pageAfter,
  presentParts,
  readsAfter,
  readsComplete,
  restoredReads,
  savedReadTimes,
  showsKept,
  showsLive,
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
    const kept = { ...previous, [commentPart("t1", "c2")]: { at: 50, kept: true } };
    const reads = readsAfter(kept, ONE, new Set([threadPart("t1")]), 200)!;
    expect(reads[threadPart("t1")]).toEqual({ at: 200, kept: false });
    expect(reads[commentPart("t1", "c2")]).toEqual({ at: 50, kept: true });
    expect(reads[LISTING_PART]).toEqual({ at: 100, kept: false });
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

describe("age and kept", () => {
  const timed = (base: ReviewThreadsPage, times: Record<string, [number, boolean]>) =>
    ({
      ...base,
      reads: Object.fromEntries(
        Object.entries(times).map(([part, [at, kept]]) => [part, { at, kept }]),
      ),
    }) as TimedThreadsPage;

  it("goes by the oldest shown thread or comment, not the listing, while any show", () => {
    const shown = timed(ONE, {
      [LISTING_PART]: [10, true],
      [threadPart("t1")]: [300, false],
      [commentPart("t1", "c1")]: [300, false],
      [commentPart("t1", "c2")]: [200, false],
    });
    expect(oldestReadAt(shown)).toBe(200);
    expect(showsKept(shown)).toBe(false);
    expect(showsLive(shown)).toBe(true);
  });

  it("goes by the listing on a page that shows no conversations", () => {
    const empty = timed(page([]), { [LISTING_PART]: [10, true] });
    expect(oldestReadAt(empty)).toBe(10);
    expect(showsKept(empty)).toBe(true);
    expect(showsLive(empty)).toBe(false);
  });

  it("tells nothing from reads that miss a part or name one not shown", () => {
    const missing = timed(ONE, { [LISTING_PART]: [10, true], [threadPart("t1")]: [10, true] });
    expect(readsComplete(missing)).toBe(false);
    expect(oldestReadAt(missing)).toBeUndefined();
    expect(showsKept(missing)).toBe(false);
    const extra = timed(page([]), { [LISTING_PART]: [10, false], [threadPart("gone")]: [1, true] });
    expect(readsComplete(extra)).toBe(false);
  });

  it("refuses times that aren't real read times", () => {
    const bad = timed(page([]), { [LISTING_PART]: [Number.NaN, false] });
    expect(readsComplete(bad)).toBe(false);
  });
});

describe("firstPageObserved", () => {
  it("observes the listing only from a first page that didn't warn", () => {
    expect(firstPageObserved(ONE).has(LISTING_PART)).toBe(true);
    expect(firstPageObserved(page([], "partial")).has(LISTING_PART)).toBe(false);
  });
});

describe("kept on this device", () => {
  it("saves a common time and lists only the parts read at another", () => {
    const fresh = pageAfter({}, ONE, presentParts(ONE), 100);
    const mixed = pageAfter(fresh, ONE, new Set([commentPart("t1", "c2")]), 200);
    expect(savedReadTimes(mixed)).toEqual({ at: 100, parts: { [commentPart("t1", "c2")]: 200 } });
    expect(savedReadTimes(fresh)).toEqual({ at: 100 });
  });

  it("restores every part kept, at its own time, replies with their own time included", () => {
    const reads = restoredReads(ONE, { at: 100, parts: { [commentPart("t1", "c2")]: 50 } })!;
    expect(reads[commentPart("t1", "c2")]).toEqual({ at: 50, kept: true });
    expect(Object.values(reads).every((read) => read.kept)).toBe(true);
    expect(Object.keys(reads)).toHaveLength(4);
  });

  it("refuses saved times naming a part the page doesn't show", () => {
    expect(restoredReads(ONE, { at: 100, parts: { [threadPart("gone")]: 50 } })).toBeUndefined();
  });

  it("restores what it saved, exactly", () => {
    const fresh = pageAfter({}, ONE, presentParts(ONE), 100);
    const mixed = pageAfter(fresh, ONE, new Set([threadPart("t1")]), 300);
    const restored = restoredReads(mixed, savedReadTimes(mixed)!)!;
    for (const [part, read] of Object.entries(mixed.reads!)) {
      expect(restored[part]).toEqual({ at: read.at, kept: true });
    }
  });
});
