import { describe, expect, it } from "vitest";

import { searchQueryFromSelection } from "./selectedText";

describe("searchQueryFromSelection", () => {
  it("takes the first non-blank line, trimmed", () => {
    expect(searchQueryFromSelection("\n   \r\n  needle here  \nsecond")).toBe("needle here");
  });

  it("makes no query from a blank selection", () => {
    expect(searchQueryFromSelection("")).toBeNull();
    expect(searchQueryFromSelection(" \t\n  ")).toBeNull();
  });

  it("makes no query from a line longer than the search routes accept", () => {
    expect(searchQueryFromSelection("x".repeat(4096))).toHaveLength(4096);
    expect(searchQueryFromSelection("x".repeat(4097))).toBeNull();
  });

  it("counts the limit in characters, as the search routes do, not UTF-16 units", () => {
    expect(searchQueryFromSelection("😀".repeat(4096))).toHaveLength(8192);
    expect(searchQueryFromSelection("😀".repeat(4097))).toBeNull();
  });
});
