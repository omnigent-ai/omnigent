import { describe, expect, it } from "vitest";

import {
  type CommentSelectionRequest,
  commentSelectionLines,
  resolveCommentSelection,
} from "./commentSelectionHandoff";

const TEXT = "alpha\nbeta gamma\ndelta\n";

function request(overrides: Partial<CommentSelectionRequest> = {}): CommentSelectionRequest {
  // "beta gamma" on line 2.
  return {
    id: "r1",
    path: "notes.txt",
    start_index: 6,
    end_index: 16,
    anchor_content: "beta gamma",
    text: TEXT,
    ...overrides,
  };
}

describe("resolveCommentSelection", () => {
  it("keeps the offsets when the file is as it was", () => {
    expect(resolveCommentSelection(request(), TEXT)).toEqual({
      kind: "apply",
      selection: { start_index: 6, end_index: 16, anchor_content: "beta gamma" },
    });
  });

  it("follows the selected text when an edit moved it", () => {
    expect(resolveCommentSelection(request(), `new line\n${TEXT}`)).toEqual({
      kind: "apply",
      selection: { start_index: 15, end_index: 25, anchor_content: "beta gamma" },
    });
  });

  it("says the lines moved when the selected text is gone", () => {
    expect(resolveCommentSelection(request(), "alpha\ndelta\n")).toEqual({ kind: "moved" });
  });

  it("says the lines moved when the file is now empty", () => {
    expect(resolveCommentSelection(request(), "")).toEqual({ kind: "moved" });
  });

  it("checks the snapshot's own offsets against its text", () => {
    // Offsets past the end of an unchanged text, or not on the anchor, are never applied.
    expect(resolveCommentSelection(request({ start_index: 40, end_index: 50 }), TEXT)).toEqual({
      kind: "moved",
    });
    expect(resolveCommentSelection(request({ start_index: 0, end_index: 10 }), TEXT)).toEqual({
      kind: "moved",
    });
  });

  it("says the lines moved for an empty selection", () => {
    expect(
      resolveCommentSelection(request({ anchor_content: "", start_index: 6, end_index: 6 }), TEXT),
    ).toEqual({ kind: "moved" });
  });
});

describe("commentSelectionLines", () => {
  it("cites the selected line", () => {
    expect(commentSelectionLines(request())).toEqual({ line: 2 });
  });

  it("cites a range across lines, not the line a selection ends at the start of", () => {
    // "beta gamma\n" ends at the start of line 3, which isn't selected.
    expect(commentSelectionLines(request({ start_index: 0, end_index: 17 }))).toEqual({
      line: 1,
      endLine: 2,
    });
  });
});
