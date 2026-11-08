import { describe, expect, it } from "vitest";

import wholeWordCases from "@/lib/__fixtures__/contentSearchWholeWord.json";
import {
  CONTENT_SEARCH_QUERY_LIMIT,
  contentSearchPattern,
  fitsContentSearchLimit,
} from "@/lib/contentSearch";

// The runner test sends each case's pattern through real ripgrep against the
// same corpus, so these patterns and the matched lines stay in step.
describe("contentSearchPattern", () => {
  it.each(wholeWordCases.cases)("$name", ({ query, regex, wholeWord, pattern, sentRegex }) => {
    expect(contentSearchPattern({ query, regex, wholeWord })).toEqual({
      pattern,
      regex: sentRegex,
    });
  });

  it("leaves every input unchanged with Whole word off", () => {
    for (const query of ["needle", "  spaced  ", "a+b", "[", "C++"]) {
      for (const regex of [false, true]) {
        expect(contentSearchPattern({ query, regex, wholeWord: false })).toEqual({
          pattern: query,
          regex,
        });
      }
    }
  });

  it("checks boundaries per code point, so astral letters count as word characters", () => {
    expect(contentSearchPattern({ query: "𝐀", regex: false, wholeWord: true }).pattern).toBe(
      "\\b𝐀\\b",
    );
    expect(contentSearchPattern({ query: "_x1", regex: false, wholeWord: true }).pattern).toBe(
      "\\b_x1\\b",
    );
  });
});

describe("fitsContentSearchLimit", () => {
  const wholeWordPattern = (query: string) =>
    contentSearchPattern({ query, regex: false, wholeWord: true }).pattern;

  it("allows a transformed query up to the server's limit and no further", () => {
    expect(wholeWordPattern("a".repeat(4092))).toHaveLength(CONTENT_SEARCH_QUERY_LIMIT);
    expect(fitsContentSearchLimit(wholeWordPattern("a".repeat(4092)))).toBe(true);
    expect(fitsContentSearchLimit(wholeWordPattern("a".repeat(4093)))).toBe(false);
  });

  it("counts the escapes a literal gains", () => {
    expect(fitsContentSearchLimit(wholeWordPattern(".".repeat(2048)))).toBe(true);
    expect(fitsContentSearchLimit(wholeWordPattern(".".repeat(2049)))).toBe(false);
  });

  it("counts an astral character once, as the server does", () => {
    const astral = "😀".repeat(CONTENT_SEARCH_QUERY_LIMIT);
    expect(astral.length).toBe(2 * CONTENT_SEARCH_QUERY_LIMIT);
    expect(fitsContentSearchLimit(astral)).toBe(true);
    expect(fitsContentSearchLimit(`${astral}x`)).toBe(false);
  });
});
