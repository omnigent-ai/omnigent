import { describe, expect, it } from "vitest";

import { foldedSummary, foldsAfterPost, opensExpanded } from "./commentFormExpansion";

const empty = { body: "", error: null, pending: false };

describe("opensExpanded", () => {
  it("stays folded for an empty draft", () => {
    expect(opensExpanded(empty)).toBe(false);
  });

  it.each([
    ["text", { body: "Half a thought" }],
    ["a post error", { error: "Rejected" }],
    ["a post under way", { pending: true }],
    ["an upload running", { uploading: 1 }],
    ["an attachment error", { attachmentError: "Couldn’t attach a.png" }],
  ])("opens for %s", (_name, facts) => {
    expect(opensExpanded({ ...empty, ...facts })).toBe(true);
  });
});

describe("foldsAfterPost", () => {
  it("folds once a post clears the draft", () => {
    expect(foldsAfterPost(true, empty)).toBe(true);
  });

  it("stays open after a failed post, while posting, or when no post ran", () => {
    expect(foldsAfterPost(true, { ...empty, error: "Rejected", body: "kept" })).toBe(false);
    expect(foldsAfterPost(true, { ...empty, pending: true })).toBe(false);
    expect(foldsAfterPost(false, empty)).toBe(false);
  });
});

describe("foldedSummary", () => {
  it("shows the invitation for an empty or blank draft", () => {
    expect(foldedSummary(empty, "Add a comment…")).toBe("Add a comment…");
    expect(foldedSummary({ ...empty, body: "\n   \n" }, "Add a comment…")).toBe("Add a comment…");
  });

  it("shows the draft's first line with text", () => {
    expect(foldedSummary({ ...empty, body: "\n  First line  \nsecond" }, "Add a comment…")).toBe(
      "First line",
    );
  });

  it("says when files are still uploading", () => {
    expect(foldedSummary({ ...empty, body: "x", uploading: 1 }, "Add")).toBe("Uploading 1 file…");
    expect(foldedSummary({ ...empty, uploading: 2 }, "Add")).toBe("Uploading 2 files…");
  });
});
