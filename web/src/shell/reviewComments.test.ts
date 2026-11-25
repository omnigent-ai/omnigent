import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import {
  openCommentsByPath,
  reviewCommentHighlights,
  reviewCommentSelectionAt,
  selectedLineRange,
} from "./reviewComments";
import { remapFormattedMarkdownComment } from "./markdownCommentRemap";

function comment(overrides: Partial<Comment> & Pick<Comment, "id" | "path">): Comment {
  return {
    conversation_id: "conv",
    body: "",
    status: "draft",
    start_index: 0,
    end_index: 3,
    anchor_content: null,
    created_at: 0,
    created_by: null,
    ...overrides,
  } as Comment;
}

describe("openCommentsByPath", () => {
  it("uses the viewer's list for the current file and re-anchors the others", () => {
    const current = comment({ id: "current", path: "a.ts" });
    const moved = comment({
      id: "moved",
      path: "b.ts",
      start_index: 0,
      end_index: 6,
      anchor_content: "needle",
    });
    const addressed = comment({ id: "done", path: "b.ts", status: "addressed" });
    const open = openCommentsByPath({
      comments: [comment({ id: "stale-current", path: "a.ts" }), moved, addressed],
      currentPath: "a.ts",
      currentComments: [current],
      currentText: (path) => (path === "b.ts" ? "xx needle\n" : undefined),
      remapFormatted: undefined,
    });
    expect(open.get("a.ts")).toEqual([current]);
    expect(open.get("b.ts")).toEqual([{ ...moved, start_index: 3, end_index: 9 }]);
  });
});

describe("openCommentsByPath with formatted Markdown anchors", () => {
  const text = "[Details](word) word wo**rd**";
  const formatted = comment({
    id: "formatted",
    path: "doc.md",
    start_index: 21,
    end_index: 27,
    anchor_content: "word",
  });
  const open = (remapFormatted: Parameters<typeof openCommentsByPath>[0]["remapFormatted"]) =>
    openCommentsByPath({
      comments: [formatted],
      currentPath: "a.ts",
      currentComments: [],
      currentText: () => text,
      remapFormatted,
    }).get("doc.md");

  it("uses the source map in other files rather than a nearby literal copy", () => {
    expect(open(remapFormattedMarkdownComment)).toEqual([formatted]);
  });

  it("keeps stored offsets while the remapper loads and searches only if it failed", () => {
    expect(open(undefined)).toEqual([formatted]);
    expect(open(null)).toEqual([{ ...formatted, start_index: 16, end_index: 20 }]);
  });
});

describe("reviewCommentHighlights", () => {
  const saved = comment({ id: "saved", path: "a.ts", start_index: 2, end_index: 5 });
  const other = comment({ id: "other", path: "b.ts", start_index: 2, end_index: 5 });
  const byPath = new Map([
    ["a.ts", [saved]],
    ["b.ts", [other]],
  ]);

  it("highlights every comment and marks the current file's active one", () => {
    expect(
      reviewCommentHighlights(byPath, "a.ts", reviewCommentSelectionAt([saved], 3) ?? null),
    ).toEqual([
      { path: "a.ts", start: 2, end: 5, active: true },
      { path: "b.ts", start: 2, end: 5, active: false },
    ]);
  });

  it("adds an unsaved selection in the current file", () => {
    const selection = { start_index: 7, end_index: 9, anchor_content: "ab" };
    expect(reviewCommentHighlights(byPath, "a.ts", selection)).toContainEqual({
      path: "a.ts",
      start: 7,
      end: 9,
      active: true,
    });
    expect(
      reviewCommentHighlights(byPath, "a.ts", { start_index: 4, end_index: 4, anchor_content: "" }),
    ).toHaveLength(2);
  });
});

describe("reviewCommentSelectionAt", () => {
  it("selects the comment covering an offset, end excluded, with its id", () => {
    const first = comment({ id: "1", path: "a.ts", start_index: 2, end_index: 5 });
    const selected = { start_index: 2, end_index: 5, anchor_content: "", comment_id: "1" };
    expect(reviewCommentSelectionAt([first], 2)).toEqual(selected);
    expect(reviewCommentSelectionAt([first], 4)).toEqual(selected);
    expect(reviewCommentSelectionAt([first], 5)).toBeUndefined();
    expect(reviewCommentSelectionAt(undefined, 3)).toBeUndefined();
  });
});

describe("selectedLineRange", () => {
  const lines = ["one\n", "two\n", "three\n"];
  it("reports the lines a selection covers", () => {
    expect(selectedLineRange(lines, 1, 6)).toEqual({ start: 1, end: 2 });
    expect(selectedLineRange(lines, 5, 6)).toEqual({ start: 2, end: 2 });
  });

  it("leaves out a line the selection only reaches the start of", () => {
    expect(selectedLineRange(lines, 0, 8)).toEqual({ start: 1, end: 2 });
    expect(selectedLineRange(lines, 0, 99)).toBeUndefined();
  });
});
