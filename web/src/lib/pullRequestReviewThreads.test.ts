import { describe, expect, it } from "vitest";
import { parsePatchFiles } from "@pierre/diffs";
import {
  mergeReviewComments,
  reviewThreadAnnotations,
  type ReviewThread,
} from "./pullRequestReviewThreads";

const file = parsePatchFiles(
  "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -5,3 +5,3 @@\n context\n-before\n+after\n tail\n",
)[0].files[0];

function thread(overrides: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id: "thread",
    path: "app.py",
    side: "RIGHT",
    line: 6,
    start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "",
    comments: { data: [], has_more: false, next_cursor: null },
    ...overrides,
  };
}

describe("review thread placement", () => {
  it("uses file coordinates and the proper side, keeping conversations on the same line distinct", () => {
    const threads = [
      thread({ id: "old", side: "LEFT" }),
      thread({ id: "new", start_line: 5 }),
      thread({ id: "second" }),
    ];
    const result = reviewThreadAnnotations(threads, file, true);
    expect(result.unplaced).toEqual([]);
    expect(
      result.annotations.map(({ side, lineNumber, metadata }) => [
        side,
        lineNumber,
        metadata.map(({ id }) => id),
      ]),
    ).toEqual([
      ["deletions", 6, ["old"]],
      ["additions", 6, ["new", "second"]],
    ]);
  });

  it.each([
    thread({ outdated: true }),
    thread({ subject_type: "FILE", line: null }),
    thread({ line: 99 }),
    thread({ line: null }),
    thread({ line: 0 }),
  ])("keeps unplaceable conversations available as fallbacks: %j", (conversation) => {
    expect(reviewThreadAnnotations([conversation], file, true)).toEqual({
      annotations: [],
      unplaced: [conversation],
    });
  });

  it("never places a thread against a different revision or an unavailable patch", () => {
    const conversation = thread();
    for (const [patch, matching] of [
      [file, false],
      [undefined, true],
    ] as const) {
      expect(reviewThreadAnnotations([conversation], patch, matching)).toEqual({
        annotations: [],
        unplaced: [conversation],
      });
    }
  });

  it("deduplicates a reply returned by both the write and the next poll", () => {
    const comment = {
      id: "reply",
      author: "me",
      body: "Thanks",
      created_at: "2026-10-28",
      url: null,
    };
    expect(mergeReviewComments([comment], [{ ...comment, body: "Updated" }])).toEqual([
      { ...comment, body: "Updated" },
    ]);
  });
});
