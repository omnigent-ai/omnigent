import { describe, expect, it } from "vitest";

import { EMPTY_CONTENT_SEARCH } from "@/lib/contentSearch";

import { textSearchOptionPressed, toggleTextSearchOption } from "./TextSearchControls";

describe("text search options", () => {
  it("flips each search option and leaves the rest", () => {
    const cased = toggleTextSearchOption(EMPTY_CONTENT_SEARCH, "case");
    expect(cased).toEqual({ ...EMPTY_CONTENT_SEARCH, caseSensitive: true });
    const word = toggleTextSearchOption(cased, "word");
    expect(textSearchOptionPressed(word, "word", false)).toBe(true);
    expect(textSearchOptionPressed(toggleTextSearchOption(word, "regex"), "regex", false)).toBe(
      true,
    );
    expect(toggleTextSearchOption(cased, "case").caseSensitive).toBe(false);
  });

  it("shows the filter on while its fields show or a glob limits the search", () => {
    expect(textSearchOptionPressed(EMPTY_CONTENT_SEARCH, "filter", false)).toBe(false);
    expect(textSearchOptionPressed(EMPTY_CONTENT_SEARCH, "filter", true)).toBe(true);
    expect(
      textSearchOptionPressed({ ...EMPTY_CONTENT_SEARCH, exclude: "*.min.js" }, "filter", false),
    ).toBe(true);
  });
});
