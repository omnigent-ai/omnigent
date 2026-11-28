import { describe, expect, it } from "vitest";

import type {
  PullRequestComment,
  PullRequestReview,
  PullRequestTimelineEvent,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";

import {
  type ActivityReads,
  type ActivityTimelineState,
  ACTIVITY_WINDOW,
  activityEntries,
  activityFilterCounts,
  activityTimeline,
  activityWindow,
  DEFAULT_ACTIVITY_TIMELINE,
  postedActivityKey,
  revealActivityEntry,
  windowIncluding,
} from "./activityTimeline";
import { postedCommentKey } from "./activityKeys";

const PR_URL = "https://github.com/acme/app/pull/7";

function comment(author: string, body: string, created: string, url: string | null = null) {
  return { author, body, created_at: created, url } satisfies PullRequestComment;
}

function thread(id: string, over: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    path: "src/app.ts",
    side: "RIGHT",
    line: 10,
    start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "",
    comments: {
      data: [
        {
          id: `${id}-1`,
          author: "ada",
          body: "Should this memoize?",
          created_at: "2026-10-02T00:00:00Z",
          url: null,
        },
      ],
      has_more: false,
      next_cursor: null,
    },
    ...over,
  };
}

function push(id: string, created: string, over: Partial<PullRequestTimelineEvent> = {}) {
  return {
    id,
    kind: "commit",
    author: "ben",
    created_at: created,
    url: null,
    sha: `${id}sha`,
    message: "Fix the list render",
    ...over,
  } satisfies PullRequestTimelineEvent;
}

function reads(over: Partial<ActivityReads> = {}): ActivityReads {
  return {
    comments: [comment("cy", "Looks good", "2026-10-01T00:00:00Z")],
    commentsPartial: false,
    threads: [thread("t1"), thread("t2", { resolved: true, path: "README.md" })],
    moreThreads: false,
    reviews: null,
    reviewsPartial: false,
    events: [push("e1", "2026-10-03T00:00:00Z")],
    eventsPartial: false,
    ...over,
  };
}

function submittedReview(
  id: string,
  state: PullRequestReview["state"],
  body: string,
  created: string,
): PullRequestReview {
  return { id, author: "dee", state, body, submitted_at: created, url: `${PR_URL}#${id}` };
}

// An empty approval, changes requested with a body (also read as a comment naming it), a
// dismissed review, and a comment-only review that only gathers its threads.
const REVIEWS = [
  submittedReview("r-ok", "APPROVED", "", "2026-10-05T00:00:00Z"),
  submittedReview("r-changes", "CHANGES_REQUESTED", "Split this up", "2026-10-06T00:00:00Z"),
  submittedReview("r-gone", "DISMISSED", "", "2026-10-07T00:00:00Z"),
  submittedReview("r-threads", "COMMENTED", "", "2026-10-08T00:00:00Z"),
];
const REVIEW_BODY: PullRequestComment = {
  ...comment("dee", "Split this up", "2026-10-06T00:00:00Z", PR_URL),
  review_id: "r-changes",
};

const state = (over: Partial<ActivityTimelineState> = {}): ActivityTimelineState => ({
  ...DEFAULT_ACTIVITY_TIMELINE,
  ...over,
});
const kinds = (timeline: ReturnType<typeof activityTimeline>) =>
  timeline.entries.map((entry) => entry.key);

describe("reviews", () => {
  const withReviews = reads({
    comments: [comment("cy", "Looks good", "2026-10-01T00:00:00Z"), REVIEW_BODY],
    reviews: REVIEWS,
  });

  it("lists each verdict, an empty approval among them, and shows a review's body once", () => {
    const timeline = activityTimeline(withReviews, state({ kind: "review", sort: "oldest" }));
    expect(
      timeline.entries.map((entry) =>
        entry.kind === "review" ? `${entry.review.state}:${entry.comment?.body ?? ""}` : entry.key,
      ),
    ).toEqual(["APPROVED:", "CHANGES_REQUESTED:Split this up", "DISMISSED:"]);
    const all = activityTimeline(withReviews, state());
    // The review's body isn't a conversation comment too.
    expect(all.counts.comment).toEqual({ known: true, count: 1, lowerBound: false });
    expect(all.counts.review).toEqual({ known: true, count: 3, lowerBound: false });
    expect(all.counts.all).toEqual({ known: true, count: 7, lowerBound: false });
    expect(
      activityTimeline({ ...withReviews, reviewsPartial: true }, state()).counts.review,
    ).toEqual({ known: true, count: 3, lowerBound: true });
  });

  it("keeps a review's body a comment, and reviews unknown, where the read brings none", () => {
    const timeline = activityTimeline({ ...withReviews, reviews: null }, state());
    expect(timeline.counts.review).toEqual({ known: false });
    expect(timeline.counts.comment).toEqual({ known: true, count: 2, lowerBound: false });
    // Nothing is missing from All: the body is a comment, and a bare verdict isn't listed.
    expect(timeline.counts.all).toEqual({ known: true, count: 5, lowerBound: false });
  });

  it("finds a review by its verdict and author, and leaves it out of a file filter", () => {
    const keys = (over: Partial<ActivityTimelineState>) =>
      kinds(activityTimeline(withReviews, state({ kind: "review", ...over })));
    expect(keys({ query: "approved" })).toEqual(["review:r-ok"]);
    expect(keys({ query: "split" })).toEqual(["review:r-changes"]);
    expect(keys({ filters: [{ field: "author", values: ["dee"] }] })).toHaveLength(3);
    expect(keys({ filters: [{ field: "file", values: ["src/app.ts"] }] })).toEqual([]);
    expect(activityFilterCounts(withReviews).author.get("dee")).toBe(3);
  });

  it("widens to every kind to reveal a review hidden by the kind shown", () => {
    const reveal = revealActivityEntry(
      withReviews,
      state({ kind: "push", query: "split" }),
      "review:r-changes",
    );
    expect(reveal?.state.kind).toBe("all");
    expect(reveal?.cleared).toEqual([]);
  });
});

describe("activityEntries", () => {
  it("lists each read entry once, with keys that stay apart where reviews share the PR's link", () => {
    // Two reviews' bodies come as comments linking to the PR, at the same time.
    const review = comment("ben", "Approving with nits", "2026-10-04T00:00:00Z", PR_URL);
    const entries = activityEntries(
      reads({ comments: [review, { ...review, author: "cy", body: "Same here" }] }),
    );
    const keys = entries.map((entry) => entry.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(entries.map((entry) => entry.kind)).toEqual([
      "comment",
      "comment",
      "thread",
      "thread",
      "push",
    ]);
  });

  it("lists no threads or pushes where the reads brought none", () => {
    const entries = activityEntries(reads({ threads: null, events: null }));
    expect(entries.map((entry) => entry.kind)).toEqual(["comment"]);
  });
});

describe("activityTimeline", () => {
  it("shows every kind newest first, counting each", () => {
    const timeline = activityTimeline(reads(), state());
    expect(kinds(timeline)).toEqual(["event:e1", "thread:t1", "thread:t2", expect.any(String)]);
    expect(timeline.entries.at(-1)?.kind).toBe("comment");
    expect(timeline.counts).toEqual({
      all: { known: true, count: 4, lowerBound: false },
      comment: { known: true, count: 1, lowerBound: false },
      thread: { known: true, count: 2, lowerBound: false },
      review: { known: false },
      push: { known: true, count: 1, lowerBound: false },
    });
    const exact = (count: number) => ({ known: true, count, lowerBound: false });
    expect(timeline.threadCounts).toEqual({
      all: exact(2),
      unresolved: exact(1),
      resolved: exact(1),
    });
  });

  it("counts what wasn't read as unknown, never as none, and what's partial as a lower bound", () => {
    const timeline = activityTimeline(
      reads({ threads: null, events: null, commentsPartial: true }),
      state(),
    );
    expect(timeline.counts.thread).toEqual({ known: false });
    expect(timeline.counts.push).toEqual({ known: false });
    expect(timeline.counts.comment).toEqual({ known: true, count: 1, lowerBound: true });
    expect(timeline.counts.all).toEqual({ known: true, count: 1, lowerBound: true });
    const more = activityTimeline(reads({ moreThreads: true, eventsPartial: true }), state());
    expect(more.counts.thread).toMatchObject({ lowerBound: true });
    expect(more.counts.push).toMatchObject({ lowerBound: true });
  });

  it("doesn't call a reply-reading filter's count exact while a thread's replies aren't all read", () => {
    // Every thread is read, but t1's later replies are still on the host.
    const unreadReplies = thread("t1", {
      comments: { ...thread("t1").comments, has_more: true },
    });
    const partial = reads({ threads: [unreadReplies], events: [], comments: [] });
    for (const narrowing of [
      { query: "needle" },
      { filters: [{ field: "author" as const, values: ["ben"] }] },
    ]) {
      const before = activityTimeline(partial, state(narrowing));
      expect(before.counts.thread).toEqual({ known: true, count: 0, lowerBound: true });
      expect(before.counts.all).toMatchObject({ count: 0, lowerBound: true });
      expect(before.threadCounts.unresolved).toMatchObject({ count: 0, lowerBound: true });
      // Once the reply that matches is read, the count is exact.
      const loaded = thread("t1", {
        comments: {
          data: [
            ...thread("t1").comments.data,
            {
              id: "t1-2",
              author: "ben",
              body: "the needle is here",
              created_at: "2026-10-02T01:00:00Z",
              url: null,
            },
          ],
          has_more: false,
          next_cursor: null,
        },
      });
      const after = activityTimeline(
        reads({ threads: [loaded], events: [], comments: [] }),
        state(narrowing),
      );
      expect(after.counts.thread).toEqual({ known: true, count: 1, lowerBound: false });
      expect(after.counts.all).toMatchObject({ count: 1, lowerBound: false });
    }
    // Unfiltered, every thread is counted whatever its replies: the total stays exact.
    expect(activityTimeline(partial, state()).counts.thread).toEqual({
      known: true,
      count: 1,
      lowerBound: false,
    });
    // A file filter reads the thread's own path, which its unread replies can't change.
    const byFile = activityTimeline(
      partial,
      state({ filters: [{ field: "file", values: ["README.md"] }] }),
    );
    expect(byFile.counts.thread).toEqual({ known: true, count: 0, lowerBound: false });
  });

  it("counts threads by state as unknown where threads weren't read", () => {
    const timeline = activityTimeline(reads({ threads: null }), state());
    expect(timeline.threadCounts).toEqual({
      all: { known: false },
      unresolved: { known: false },
      resolved: { known: false },
    });
  });

  it("shows one kind at a time, keeping every kind's count", () => {
    const timeline = activityTimeline(reads(), state({ kind: "thread" }));
    expect(kinds(timeline)).toEqual(["thread:t1", "thread:t2"]);
    expect(timeline.counts.comment).toMatchObject({ count: 1 });
  });

  it("narrows threads by state without hiding comments or pushes", () => {
    const open = activityTimeline(reads(), state({ threads: "unresolved" }));
    expect(open.entries.map((entry) => entry.kind)).toEqual(["push", "thread", "comment"]);
    expect(open.entries.find((entry) => entry.kind === "thread")?.key).toBe("thread:t1");
    const resolved = activityTimeline(reads(), state({ kind: "thread", threads: "resolved" }));
    expect(kinds(resolved)).toEqual(["thread:t2"]);
  });

  it("filters pushes by author, and leaves them out of a file or state filter", () => {
    const byAuthor = activityTimeline(
      reads(),
      state({ filters: [{ field: "author", values: ["ben"] }] }),
    );
    expect(kinds(byAuthor)).toEqual(["event:e1"]);
    const byFile = activityTimeline(
      reads(),
      state({ filters: [{ field: "file", values: ["README.md"] }] }),
    );
    expect(kinds(byFile)).toEqual(["thread:t2"]);
    expect(byFile.counts.push).toEqual({ known: true, count: 0, lowerBound: false });
  });

  it("searches pushes by message and commit as it searches comments and threads", () => {
    expect(kinds(activityTimeline(reads(), state({ query: "list render" })))).toEqual(["event:e1"]);
    expect(kinds(activityTimeline(reads(), state({ query: "e1sha" })))).toEqual(["event:e1"]);
    expect(kinds(activityTimeline(reads(), state({ query: "memoize" })))).toEqual([
      "thread:t1",
      "thread:t2",
    ]);
  });

  it("sorts oldest first and by file in the Comments page's orders", () => {
    const oldest = activityTimeline(reads(), state({ sort: "oldest" }));
    expect(oldest.entries.map((entry) => entry.kind)).toEqual([
      "comment",
      "thread",
      "thread",
      "push",
    ]);
    // By file: entries without a file first, in time order, then threads by path.
    const byFile = activityTimeline(reads(), state({ sort: "file" }));
    expect(byFile.entries.map((entry) => entry.key)).toEqual([
      expect.stringMatching(/^comment:/),
      "event:e1",
      "thread:t2",
      "thread:t1",
    ]);
  });

  it("keeps a stable order for entries at the same time", () => {
    const same = "2026-10-05T00:00:00Z";
    const tied = reads({
      comments: [comment("cy", "One", same)],
      threads: [thread("t1", { comments: { ...thread("t1").comments, data: [] } })],
      events: [push("e1", same), push("e2", same)],
    });
    const first = kinds(activityTimeline(tied, state()));
    expect(kinds(activityTimeline(tied, state()))).toEqual(first);
  });
});

describe("revealActivityEntry", () => {
  it("leaves a state that already shows the entry as it is", () => {
    expect(revealActivityEntry(reads(), state({ kind: "thread" }), "thread:t1")).toEqual({
      state: state({ kind: "thread" }),
      cleared: [],
    });
  });

  it("widens a kind or thread state that hides the entry, which isn't reported as cleared", () => {
    const pushes = revealActivityEntry(reads(), state({ kind: "push" }), "thread:t2")!;
    expect(pushes).toEqual({ state: state({ kind: "all" }), cleared: [] });
    const open = revealActivityEntry(
      reads(),
      state({ kind: "thread", threads: "unresolved" }),
      "thread:t2",
    )!;
    expect(open.state).toEqual(state({ kind: "thread", threads: "all" }));
  });

  it("clears only the filters and search that leave the entry out, and names them", () => {
    const filters = [
      { field: "file" as const, values: ["README.md"] },
      { field: "author" as const, values: ["ada"] },
    ];
    const revealed = revealActivityEntry(
      reads(),
      state({ filters, query: "memoize" }),
      "thread:t1",
    )!;
    expect(revealed.cleared).toEqual(["file"]);
    expect(revealed.state.filters).toEqual([filters[1]]);
    expect(revealed.state.query).toBe("memoize");
    const pushShown = revealActivityEntry(
      reads(),
      state({ filters, query: "memoize" }),
      "event:e1",
    )!;
    expect(pushShown.cleared).toEqual(["file", "author", "search"]);
    expect(pushShown.state).toEqual(state());
    expect(kinds(activityTimeline(reads(), pushShown.state))).toContain("event:e1");
  });

  it("is null for a key nothing read has", () => {
    expect(revealActivityEntry(reads(), state(), "comment:missing")).toBeNull();
  });
});

describe("postedActivityKey", () => {
  it("names a posted comment's entry once the read includes it, and nothing before", () => {
    const posted = comment("cy", "Thanks!", "2026-10-06T00:00:00Z", "https://example.test/c/9");
    const key = postedCommentKey(posted);
    expect(postedActivityKey(reads(), key)).toBeNull();
    const withPosted = reads({ comments: [...reads().comments, posted] });
    const entry = activityEntries(withPosted).find(
      (candidate) => candidate.kind === "comment" && candidate.comment === posted,
    );
    expect(postedActivityKey(withPosted, key)).toBe(entry!.key);
  });
});

describe("activityWindow", () => {
  const many = reads({
    comments: Array.from({ length: 13 }, (_, index) =>
      comment("cy", `Comment ${index}`, `2026-10-${String(index + 10).padStart(2, "0")}T00:00:00Z`),
    ),
    threads: [],
    events: [],
  });

  it("shows the newest entries, leaving the earlier ones out at the end of newest first", () => {
    const entries = activityTimeline(many, state()).entries;
    const window = activityWindow(entries, "newest", ACTIVITY_WINDOW);
    expect(window.shown).toEqual(entries.slice(0, ACTIVITY_WINDOW));
    expect(window).toMatchObject({ hidden: 3, hiddenAt: "end" });
  });

  it("keeps the newest at the end of oldest first, leaving the earlier ones out at the start", () => {
    const entries = activityTimeline(many, state({ sort: "oldest" })).entries;
    const window = activityWindow(entries, "oldest", ACTIVITY_WINDOW);
    expect(window.shown).toEqual(entries.slice(3));
    expect(window).toMatchObject({ hidden: 3, hiddenAt: "start" });
  });

  it("leaves nothing out of a short timeline", () => {
    const entries = activityTimeline(reads(), state()).entries;
    expect(activityWindow(entries, "newest", ACTIVITY_WINDOW)).toEqual({
      shown: entries,
      hidden: 0,
      hiddenAt: "end",
    });
  });

  it("widens to include an entry outside the window, in either order", () => {
    const newest = activityTimeline(many, state()).entries;
    const oldestEntry = newest.at(-1)!.key;
    expect(windowIncluding(newest, "newest", ACTIVITY_WINDOW, oldestEntry)).toBe(13);
    const oldest = activityTimeline(many, state({ sort: "oldest" })).entries;
    expect(windowIncluding(oldest, "oldest", ACTIVITY_WINDOW, oldestEntry)).toBe(13);
    expect(windowIncluding(newest, "newest", ACTIVITY_WINDOW, newest[0]!.key)).toBe(
      ACTIVITY_WINDOW,
    );
    expect(windowIncluding(newest, "newest", ACTIVITY_WINDOW, "comment:missing")).toBe(
      ACTIVITY_WINDOW,
    );
  });
});
