import { describe, expect, it } from "vitest";
import { parsePatchFiles } from "@pierre/diffs";
import {
  mergeReviewComments,
  reviewThreadAnnotations,
  reviewSelection,
  reviewSuggestionSource,
  reviewSuggestionDiff,
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
  it.each([
    [
      { start: 6, end: 6, side: "additions" },
      { side: "RIGHT", line: 6, start_line: null },
    ],
    [
      { start: 7, end: 5, side: "deletions" },
      { side: "LEFT", line: 7, start_line: 5 },
    ],
    [
      { start: 5, end: 7, side: "additions" },
      { side: "RIGHT", line: 7, start_line: 5 },
    ],
    [{ start: 5, end: 7, side: "deletions", endSide: "additions" }, null],
    [{ start: 4, end: 6, side: "additions" }, null],
    [{ start: 6, end: 99, side: "additions" }, null],
    [{ start: 0, end: 1, side: "additions" }, null],
    [{ start: 5.5, end: 6, side: "additions" }, null],
  ] as const)("maps or rejects a selected range: %j", (selection, expected) => {
    expect(reviewSelection(selection, file)).toEqual(expected);
  });
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

describe("review suggestion source", () => {
  it.each(["new\n\ttext  \n", "", "before\nkeep"])(
    "builds a library diff for a complete replacement: %j",
    (replacement) => {
      const diff = reviewSuggestionDiff(
        "path with spaces/app.py",
        {
          line: 47,
          lines: ["before", "keep"],
          original: false,
        },
        replacement,
      );
      expect(diff.name).toBe("path with spaces/app.py");
      expect(diff.deletionLines).toEqual(["before\n", "keep\n"]);
      const added = replacement === "" ? [] : replacement.split("\n").map((line) => `${line}\n`);
      expect(diff.additionLines).toEqual(added);
      expect(diff.hunks).toHaveLength(1);
      expect(diff.hunks[0]).toMatchObject({
        deletionStart: 47,
        deletionCount: 2,
        additionCount: added.length,
        collapsedBefore: 0,
        hunkContent: [{ type: "change", deletions: 2, additions: added.length }],
      });
      if (added.length) expect(diff.hunks[0].additionStart).toBe(47);
    },
  );

  it.each([
    ["RIGHT", null, ["after"]],
    ["LEFT", null, ["before"]],
    ["RIGHT", 5, ["context", "after"]],
    ["LEFT", 5, ["context", "before"]],
    ["RIGHT", 6, ["after"]],
  ] as const)("reads %s lines from %s to the thread endpoint", (side, start_line, expected) => {
    expect(reviewSuggestionSource(thread({ side, start_line }), file)).toEqual({
      line: start_line ?? 6,
      lines: expected,
      original: false,
    });
  });

  it("uses hunk offsets rather than file line numbers as array indexes", () => {
    const patch = parsePatchFiles(
      "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -5 +5 @@\n-old\n+first\n@@ -20,2 +20,2 @@\n-before\n+\tcurrent  \n \n",
    )[0].files[0];
    expect(reviewSuggestionSource(thread({ line: 21, start_line: 20 }), patch)).toEqual({
      line: 20,
      lines: ["\tcurrent  ", ""],
      original: false,
    });
  });

  it("can read expanded context and the current name of a renamed file", () => {
    const expanded = {
      ...file,
      name: "renamed.py",
      prevName: "app.py",
      isPartial: false,
      additionLines: ["outside\r\n", "context\n"],
    };
    expect(reviewSuggestionSource(thread({ line: 1 }), expanded)).toEqual({
      line: 1,
      lines: ["outside"],
      original: false,
    });
  });

  it.each([
    { line: 99 },
    { start_line: 1 },
    { start_line: 7 },
    { start_side: "LEFT" as const, start_line: 5 },
    { subject_type: "FILE" as const },
    { path: "unrelated.py" },
    { outdated: true },
  ])("does not invent before text when the range is unavailable: %j", (overrides) => {
    expect(reviewSuggestionSource(thread(overrides), file)).toBeNull();
  });

  it("labels the saved hunk and uses original coordinates for an outdated range", () => {
    const original = thread({
      outdated: true,
      line: 99,
      original_line: 12,
      original_start_line: 11,
      diff_hunk: "@@ -10,4 +10,4 @@\n context\n-first\n+original first\n original second",
    });
    expect(reviewSuggestionSource(original, file)).toEqual({
      line: 11,
      lines: ["original first", "original second"],
      original: true,
    });
    expect(reviewSuggestionSource({ ...original, original_line: 13 }, file)).toBeNull();
    expect(
      reviewSuggestionSource({ ...original, original_start_line: undefined }, file),
    ).toBeNull();
    expect(reviewSuggestionSource({ ...original, diff_hunk: "unavailable" }, file)).toBeNull();
  });
});
