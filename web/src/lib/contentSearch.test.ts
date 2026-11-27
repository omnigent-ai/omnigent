import { afterEach, describe, expect, it, vi } from "vitest";

import wholeWordCases from "@/lib/__fixtures__/contentSearchWholeWord.json";
import {
  CONTENT_SEARCH_QUERY_LIMIT,
  contentMatchRanges,
  contentSearchPattern,
  contentSearchProblem,
  type ContentSearchInput,
  EMPTY_CONTENT_SEARCH,
  fetchContentSearch,
  fitsContentSearchLimit,
  runContentSearch,
  wholeWordRegexProblem,
} from "@/lib/contentSearch";
import { authenticatedFetch } from "@/lib/identity";
import { searchTextViaWorkspaceApi } from "@/lib/workspaceApi";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/workspaceApi", () => ({ searchTextViaWorkspaceApi: vi.fn() }));

afterEach(() => {
  vi.resetAllMocks();
});

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

describe("fetchContentSearch", () => {
  const options = { caseSensitive: false, regex: true, include: "src/**", exclude: "" };
  const found = {
    matches: [{ path: "a.ts", line: 1, column: 1, text: "needle", match_start: 0, match_end: 6 }],
    truncated: false,
    warning: null,
  };

  it("uses the workspace API's answer without REST", async () => {
    vi.mocked(searchTextViaWorkspaceApi).mockResolvedValue(found);

    expect(await fetchContentSearch("s1", "ne+dle", options)).toEqual(found);

    expect(searchTextViaWorkspaceApi).toHaveBeenCalledWith("s1", "ne+dle", options, undefined);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("falls back to REST, which reports an invalid search", async () => {
    vi.mocked(searchTextViaWorkspaceApi).mockResolvedValue(null);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json(found));
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      Response.json({ error: "regex parse error" }),
    );

    expect(await fetchContentSearch("s1", "ne+dle", options)).toEqual(found);
    await expect(fetchContentSearch("s1", "[", options)).rejects.toThrow("regex parse error");

    const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0]![0]), "http://test");
    expect(url.pathname).toBe("/v1/sessions/s1/resources/environments/default/content-search");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: "ne+dle",
      case_sensitive: "false",
      regex: "true",
      include: "src/**",
      exclude: "",
    });
  });
});

const input = (overrides: Partial<ContentSearchInput> = {}): ContentSearchInput => ({
  ...EMPTY_CONTENT_SEARCH,
  ...overrides,
});

describe("contentSearchProblem", () => {
  it("accepts any search without Whole word", () => {
    expect(contentSearchProblem(input({ query: "foo)|(bar", regex: true }))).toBeNull();
    expect(
      contentSearchProblem(input({ query: "x".repeat(CONTENT_SEARCH_QUERY_LIMIT) })),
    ).toBeNull();
  });

  it("says how to fix a Whole word search that can't keep its boundaries", () => {
    expect(
      contentSearchProblem(input({ query: "foo)|(bar", regex: true, wholeWord: true })),
    ).toMatch(/closes a group it never opened/);
    expect(contentSearchProblem(input({ query: "(?x)a b", regex: true, wholeWord: true }))).toMatch(
      /verbose mode/,
    );
    expect(
      contentSearchProblem(
        input({ query: "é".repeat(CONTENT_SEARCH_QUERY_LIMIT), wholeWord: true }),
      ),
    ).toMatch(/too long to match whole words/);
    expect(contentSearchProblem(input({ query: "needle", wholeWord: true }))).toBeNull();
  });
});

describe("runContentSearch", () => {
  it("sends the search as typed, with Whole word as its pattern", async () => {
    const result = { matches: [], truncated: false, warning: null };
    vi.mocked(searchTextViaWorkspaceApi).mockResolvedValue(result);
    await expect(
      runContentSearch("conv", input({ query: "needle", wholeWord: true, include: "src/**" })),
    ).resolves.toBe(result);
    expect(searchTextViaWorkspaceApi).toHaveBeenCalledWith(
      "conv",
      "\\bneedle\\b",
      { caseSensitive: false, regex: true, include: "src/**", exclude: "" },
      undefined,
    );
  });
});

describe("contentMatchRanges", () => {
  const line = (text: string, start: number, end: number) => ({
    text,
    match_start: start,
    match_end: end,
  });

  it("marks every plain-text occurrence, ignoring case unless asked", () => {
    const found = line("Needle, needle, NEEDLE", 0, 6);
    expect(contentMatchRanges(found, input({ query: "needle" }))).toEqual([
      { start: 0, end: 6 },
      { start: 8, end: 14 },
      { start: 16, end: 22 },
    ]);
    expect(
      contentMatchRanges(
        line("Needle, needle", 8, 14),
        input({ query: "needle", caseSensitive: true }),
      ),
    ).toEqual([{ start: 8, end: 14 }]);
  });

  it("matches whole words only at word boundaries, including Unicode letters", () => {
    const found = line("needle needles éneedle needle_x needle", 0, 6);
    expect(contentMatchRanges(found, input({ query: "needle", wholeWord: true }))).toEqual([
      { start: 0, end: 6 },
      { start: 32, end: 38 },
    ]);
    // Punctuation edges need no boundary, as in the search engines.
    expect(
      contentMatchRanges(line("a C++ b C++", 2, 5), input({ query: "C++", wholeWord: true })),
    ).toEqual([
      { start: 2, end: 5 },
      { start: 8, end: 11 },
    ]);
  });

  it("finds a whole word that overlaps a rejected candidate", () => {
    expect(
      contentMatchRanges(line("xa-a-a", 3, 6), input({ query: "a-a", wholeWord: true })),
    ).toEqual([{ start: 3, end: 6 }]);
    expect(
      contentMatchRanges(line("xa-a-a a-a", 3, 6), input({ query: "a-a", wholeWord: true })),
    ).toEqual([
      { start: 3, end: 6 },
      { start: 7, end: 10 },
    ]);
  });

  it("counts word characters as the search engines' Unicode \\w does", () => {
    // A superscript digit isn't a word character; a zero-width joiner or a combining letter is.
    const found = line("needle needle² needle\u200C needle\u200D needleᢅ", 0, 6);
    expect(contentMatchRanges(found, input({ query: "needle", wholeWord: true }))).toEqual([
      { start: 0, end: 6 },
      { start: 7, end: 13 },
    ]);
    // A letterlike numeral is alphabetic, so it joins the word; ripgrep finds only the second.
    expect(
      contentMatchRanges(
        line("Ⅻneedle ²needle", 9, 15),
        input({ query: "needle", wholeWord: true }),
      ),
    ).toEqual([{ start: 9, end: 15 }]);
  });

  it("does not confirm a whole word against a cropped edge", () => {
    const found = line("…needle, needle, needle…", 9, 15);
    expect(contentMatchRanges(found, input({ query: "needle", wholeWord: true }))).toEqual([
      { start: 9, end: 15 },
    ]);
    expect(contentMatchRanges(found, input({ query: "needle" }))).toEqual([
      { start: 1, end: 7 },
      { start: 9, end: 15 },
      { start: 17, end: 23 },
    ]);
  });

  it("treats regular-expression characters in plain text literally", () => {
    const found = line("a.b axb a.b (x) [y]", 0, 3);
    expect(contentMatchRanges(found, input({ query: "a.b" }))).toEqual([
      { start: 0, end: 3 },
      { start: 8, end: 11 },
    ]);
    expect(contentMatchRanges(line("(x) [y]", 0, 3), input({ query: "(x)" }))).toEqual([
      { start: 0, end: 3 },
    ]);
    expect(contentMatchRanges(line("x/y-z x/y-z", 0, 5), input({ query: "x/y-z" }))).toHaveLength(
      2,
    );
  });

  it("keeps only the reported range for a regular expression", () => {
    const found = line("foo1 foo2 foo3", 5, 9);
    expect(contentMatchRanges(found, input({ query: "foo\\d", regex: true }))).toEqual([
      { start: 5, end: 9 },
    ]);
  });

  it("keeps the reported range when an overlapping occurrence starts earlier", () => {
    expect(contentMatchRanges(line("aaa", 1, 3), input({ query: "aa" }))).toEqual([
      { start: 1, end: 3 },
    ]);
  });

  it("reports UTF-16 offsets past astral characters", () => {
    expect(contentMatchRanges(line("🚀 go 🚀 go", 3, 5), input({ query: "go" }))).toEqual([
      { start: 3, end: 5 },
      { start: 9, end: 11 },
    ]);
  });

  it("returns nothing for an empty reported range", () => {
    expect(contentMatchRanges(line("x", 0, 0), input({ query: "", regex: true }))).toEqual([]);
  });
});
