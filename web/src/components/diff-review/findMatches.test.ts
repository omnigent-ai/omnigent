import { describe, expect, it } from "vitest";
import { DEFAULT_FIND_OPTIONS } from "@/components/FindBar";
import {
  findQueryFromSelection,
  findReviewMatches,
  findStatusLabel,
  MAXIMUM_FIND_MATCHES,
  type ReviewSearchLine,
} from "./findMatches";

const lines: ReviewSearchLine[] = [
  { id: "diff:a.ts", text: "a.ts", lineNumber: 1, target: "currentPath" },
  { id: "diff:a.ts", text: "const Value = value + values;\n", lineNumber: 3, side: "additions" },
  { id: "diff:b.ts", text: "return value_2;", lineNumber: 9, side: "deletions" },
];

function ranges(query: string, options = DEFAULT_FIND_OPTIONS) {
  return findReviewMatches(lines, query, options).matches.map(
    (match) => `${match.id}:${match.lineNumber}:${match.from}-${match.to}`,
  );
}

describe("findReviewMatches", () => {
  it("ignores case by default and reports every occurrence in order", () => {
    expect(ranges("value")).toEqual([
      "diff:a.ts:3:6-11",
      "diff:a.ts:3:14-19",
      "diff:a.ts:3:22-27",
      "diff:b.ts:9:7-12",
    ]);
  });

  it("matches case when asked", () => {
    expect(ranges("Value", { ...DEFAULT_FIND_OPTIONS, caseSensitive: true })).toEqual([
      "diff:a.ts:3:6-11",
    ]);
  });

  it("requires word boundaries for whole words", () => {
    expect(ranges("value", { ...DEFAULT_FIND_OPTIONS, wholeWord: true })).toEqual([
      "diff:a.ts:3:6-11",
      "diff:a.ts:3:14-19",
    ]);
  });

  it("treats the query literally unless regular expressions are on", () => {
    expect(ranges("value.")).toEqual([]);
    expect(ranges("value.", { ...DEFAULT_FIND_OPTIONS, regexp: true })).toEqual([
      "diff:a.ts:3:6-12",
      "diff:a.ts:3:14-20",
      "diff:a.ts:3:22-28",
      "diff:b.ts:9:7-13",
    ]);
  });

  it("finds file paths and keeps their target", () => {
    const [match] = findReviewMatches(lines, "a.ts", DEFAULT_FIND_OPTIONS).matches;
    expect(match).toMatchObject({ target: "currentPath", from: 0, to: 4 });
  });

  it("never matches the line ending or an empty range", () => {
    expect(ranges("\n", { ...DEFAULT_FIND_OPTIONS, regexp: true })).toEqual([]);
    expect(ranges("^", { ...DEFAULT_FIND_OPTIONS, regexp: true })).toEqual([]);
  });

  it("steps over astral characters after an empty match", () => {
    const astral: ReviewSearchLine[] = [{ id: "diff:e.ts", text: "😀a😀\n", lineNumber: 1 }];
    const regexp = { ...DEFAULT_FIND_OPTIONS, regexp: true };
    expect(findReviewMatches(astral, "(?:)", regexp).matches).toEqual([]);
    expect(
      findReviewMatches(astral, "a?", regexp).matches.map(({ from, to }) => [from, to]),
    ).toEqual([[2, 3]]);
  });

  it("reports an invalid expression instead of throwing", () => {
    const result = findReviewMatches(lines, "(", { ...DEFAULT_FIND_OPTIONS, regexp: true });
    expect(result).toEqual({ valid: false, truncated: false, matches: [] });
    expect(findStatusLabel("(", result, 0)).toBe("Invalid expression");
  });

  it("stops at the match limit and says so", () => {
    const many = Array.from({ length: MAXIMUM_FIND_MATCHES + 5 }, (_, index) => ({
      id: "diff:big.ts",
      text: "x",
      lineNumber: index + 1,
    }));
    const result = findReviewMatches(many, "x", DEFAULT_FIND_OPTIONS);
    expect(result.truncated).toBe(true);
    expect(result.matches).toHaveLength(MAXIMUM_FIND_MATCHES);
    expect(findStatusLabel("x", result, 0)).toBe(`1 of ${MAXIMUM_FIND_MATCHES}+`);
  });

  it("finds the one match in a million-line review", () => {
    const million = Array.from({ length: 1_000_000 }, (_, index) => ({
      id: `diff:File${index % 1000}.ts`,
      text: `export const value${index} = "large review";`,
      lineNumber: index + 1,
      side: "additions" as const,
    }));
    const result = findReviewMatches(million, "value999999 ", DEFAULT_FIND_OPTIONS);
    expect(result.matches).toHaveLength(1);
    expect(result.matches[0]).toMatchObject({ lineNumber: 1_000_000, id: "diff:File999.ts" });
    // Correctness only: a wall-clock budget would fail on a slow or instrumented run.
  }, 60_000);
});

describe("find labels", () => {
  it("describes the current position", () => {
    const result = findReviewMatches(lines, "value", DEFAULT_FIND_OPTIONS);
    expect(findStatusLabel("", result, 0)).toBe("");
    expect(findStatusLabel("value", result, 1)).toBe("2 of 4");
    expect(findStatusLabel("zzz", findReviewMatches(lines, "zzz", DEFAULT_FIND_OPTIONS), 0)).toBe(
      "No results",
    );
  });

  it("turns a multi-line selection into one line", () => {
    expect(findQueryFromSelection("  foo\n   bar\r\nbaz ")).toBe("foo bar baz");
  });
});
