import { describe, expect, it } from "vitest";

import wholeWordCases from "@/lib/__fixtures__/contentSearchWholeWord.json";
import {
  CONTENT_SEARCH_QUERY_LIMIT,
  contentSearchPattern,
  fitsContentSearchLimit,
  wholeWordRegexProblem,
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

  it("leaves a non-ASCII edge, read per code point, for ripgrep to classify", () => {
    // Ripgrep's Unicode tables, not the browser's, decide whether this needs a boundary.
    expect(contentSearchPattern({ query: "𝐀", regex: false, wholeWord: true }).pattern).toBe(
      "(?:\\b[\\x{1D400}&&\\w]\\b|[\\x{1D400}&&\\W])",
    );
    expect(contentSearchPattern({ query: "_x1", regex: false, wholeWord: true }).pattern).toBe(
      "\\b_x1\\b",
    );
  });
});

describe("wholeWordRegexProblem", () => {
  it("finds a group closed before it is opened, which would escape the wrapper", () => {
    // `\b(?:foo)|(bar)\b` would match inside `foobar`, `xbar` and `foox`.
    for (const query of [
      "foo)|(bar",
      "foo))|((bar",
      "(a)b)",
      "[ ])",
      "[^^])",
      // A capture name may contain `[`, which does not open a class there.
      "(?P<a[>foo))|((bar)",
      "(?<a[>foo))|((bar)",
    ]) {
      expect(wholeWordRegexProblem(query)).toBe("unopened-group");
    }
  });

  it("accepts balanced groups and parentheses ripgrep reads as literals", () => {
    for (const query of [
      "needle|pin",
      "(?i)foo",
      "(?i:foo)|bar",
      "(?-x)foo",
      "(?P<x>foo)",
      "(?<a[]>foo)|bar",
      "(?P<a]>[)])",
      "(a|b)(c)",
      "a\\)b",
      "\\(?x\\)",
      "[)]",
      "[(?x)]",
      "[])]",
      "[^])]",
      "[]x)]",
      "[[)]a]",
      "[[:alpha:])]",
      "(unclosed",
    ]) {
      expect(wholeWordRegexProblem(query)).toBeNull();
    }
  });

  it("refuses verbose mode, where whitespace changes how brackets read", () => {
    // Ripgrep reads `[ ](]` as one class here, so a later `)` closes the wrapper.
    for (const query of [
      "(?x)[ ](]foo)|(bar",
      "(?x)[ ^ ](]foo)|(bar",
      "(?x)[ ])]",
      "(?ix)foo",
      "(?x:foo)",
      "(?x-i)foo",
      "a(?x)b",
      "(?P<a[>foo)(?x)[ ](]foo)|(bar",
    ]) {
      expect(wholeWordRegexProblem(query)).toBe("verbose");
    }
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

  it("counts the wrapper a regular expression gains", () => {
    const wrapped = (query: string) =>
      contentSearchPattern({ query, regex: true, wholeWord: true }).pattern;
    expect(wrapped("a".repeat(4088))).toHaveLength(CONTENT_SEARCH_QUERY_LIMIT);
    expect(fitsContentSearchLimit(wrapped("a".repeat(4088)))).toBe(true);
    expect(fitsContentSearchLimit(wrapped("a".repeat(4089)))).toBe(false);
  });

  it("counts the classification a non-ASCII edge gains", () => {
    // Everything but the middle: the two classified edges.
    const edges = wholeWordPattern("éé").length;
    const room = CONTENT_SEARCH_QUERY_LIMIT - edges;
    expect(fitsContentSearchLimit(wholeWordPattern(`é${"a".repeat(room)}é`))).toBe(true);
    expect(fitsContentSearchLimit(wholeWordPattern(`é${"a".repeat(room + 1)}é`))).toBe(false);
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
