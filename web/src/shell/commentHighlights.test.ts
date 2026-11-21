import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { commentHighlights, commentSelectionAt } from "./commentHighlights";

function comment(start: number, end: number, anchor = "anchor"): Comment {
  return {
    id: `c_${start}`,
    conversation_id: "conv_1",
    path: "notes.md",
    start_index: start,
    end_index: end,
    body: "body",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: anchor,
  } as Comment;
}

describe("commentHighlights", () => {
  it("marks the comment matching the active selection as active", () => {
    expect(
      commentHighlights([comment(0, 4), comment(6, 9)], {
        start_index: 6,
        end_index: 9,
        anchor_content: "",
      }),
    ).toEqual([
      { start: 0, end: 4, active: false },
      { start: 6, end: 9, active: true },
    ]);
  });

  it("adds an unsaved selection as an active highlight", () => {
    expect(
      commentHighlights([comment(0, 4)], { start_index: 5, end_index: 8, anchor_content: "" }),
    ).toEqual([
      { start: 0, end: 4, active: false },
      { start: 5, end: 8, active: true },
    ]);
  });

  it("skips an empty active selection", () => {
    expect(commentHighlights([], { start_index: 3, end_index: 3, anchor_content: "" })).toEqual([]);
  });
});

describe("commentSelectionAt", () => {
  it("finds the comment containing an offset, excluding its end", () => {
    const comments = [comment(2, 5, "llo")];
    expect(commentSelectionAt(comments, 2)).toEqual({
      start_index: 2,
      end_index: 5,
      anchor_content: "llo",
    });
    expect(commentSelectionAt(comments, 5)).toBeUndefined();
    expect(commentSelectionAt(comments, 1)).toBeUndefined();
  });
});
