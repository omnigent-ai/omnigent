import { describe, expect, it } from "vitest";

import { MatchClass, type MatchStrategy, foldText, matchField, mergeRanges } from "./match";

function match(query: string, text: string, matching?: MatchStrategy) {
  return matchField(foldText(query).values, foldText(text), 0, matching);
}

function classOf(query: string, text: string): number | undefined {
  return match(query, text)?.matchClass;
}

describe("matchField", () => {
  it("classifies exact, prefix, boundary substring, substring, and subsequence matches", () => {
    expect(classOf("open", "Open")).toBe(MatchClass.exact);
    expect(classOf("ope", "Open settings")).toBe(MatchClass.prefix);
    expect(classOf("set", "Open settings")).toBe(MatchClass.boundarySubstring);
    expect(classOf("ttin", "Open settings")).toBe(MatchClass.substring);
    expect(classOf("opst", "Open settings")).toBe(MatchClass.subsequence);
  });

  it("returns null when characters are missing or out of order", () => {
    expect(classOf("xyz", "Open settings")).toBeUndefined();
    expect(classOf("tsop", "Open settings")).toBeUndefined();
  });

  it("returns null for an empty query, an empty field, or a query longer than the field", () => {
    expect(match("", "Open")).toBeNull();
    expect(match("open", "")).toBeNull();
    expect(match("opened", "Open")).toBeNull();
  });

  it("only allows substrings for contiguous fields", () => {
    expect(match("opst", "Open settings", "contiguous")).toBeNull();
    expect(match("sett", "Open settings", "contiguous")?.matchClass).toBe(
      MatchClass.boundarySubstring,
    );
  });

  it("ranks shorter fields and earlier substrings first within a class", () => {
    expect(match("open", "Open file")!.penalty).toBeLessThan(match("open", "Open folder")!.penalty);
    expect(match("file", "a file")!.penalty).toBeLessThan(match("file", "the file")!.penalty);
  });

  it("passes the field weight through for callers to break ties", () => {
    expect(matchField(foldText("open").values, foldText("Open"), 7)?.weight).toBe(7);
  });

  it("folds case and diacritics while reporting source ranges", () => {
    expect(match("cafe", "Le Café")?.ranges).toEqual([{ start: 3, end: 7 }]);
  });

  it("keeps a decomposed accent with its letter", () => {
    expect(match("cafe", "Le Café noir")?.ranges).toEqual([{ start: 3, end: 8 }]);
  });

  it("maps ranges past astral characters to UTF-16 offsets", () => {
    expect(match("go", "🚀 go")?.ranges).toEqual([{ start: 3, end: 5 }]);
  });

  it("merges the characters of a subsequence into runs", () => {
    expect(match("opse", "Open settings")?.ranges).toEqual([
      { start: 0, end: 2 },
      { start: 5, end: 7 },
    ]);
  });

  it("prefers a compact match in the last path component", () => {
    const filename = match("qp", "src/ui/quickPick.tsx");
    const directory = match("qp", "quick/pages/index.ts");
    expect(filename).not.toBeNull();
    expect(directory).not.toBeNull();
    expect(filename!.penalty).toBeLessThan(directory!.penalty);
  });

  it("falls back to the whole path when the file name lacks the letters", () => {
    expect(match("srcx", "src/ui/index.ts")?.ranges).toEqual([
      { start: 0, end: 3 },
      { start: 11, end: 12 },
    ]);
  });

  it("prefers a later substring at a word start to an earlier one inside a word", () => {
    for (const matching of ["fuzzy", "contiguous"] as const) {
      const reset = match("set", "Reset settings", matching);
      expect(reset?.matchClass).toBe(MatchClass.boundarySubstring);
      expect(reset?.ranges).toEqual([{ start: 6, end: 9 }]);
      expect(match("set", "Reset", matching)).toMatchObject({
        matchClass: MatchClass.substring,
        ranges: [{ start: 2, end: 5 }],
      });
    }
  });

  it("finds a later compact subsequence instead of the first scattered one", () => {
    // The first "a" leads to a scattered match; the later "ab" is adjacent.
    expect(match("abc", "a-x-b-x-c--ab-c")).toMatchObject({
      matchClass: MatchClass.subsequence,
      ranges: [
        { start: 11, end: 13 },
        { start: 14, end: 15 },
      ],
    });
  });

  it("still tries the last start when there are more starts than the scan bound", () => {
    const text = `${"a-x-".repeat(20)}ab-c`;
    expect(match("abc", text)?.ranges).toEqual([
      { start: 80, end: 82 },
      { start: 83, end: 84 },
    ]);
  });
});

describe("mergeRanges", () => {
  it("merges overlapping and adjacent ranges in order", () => {
    expect(
      mergeRanges([
        { start: 5, end: 6 },
        { start: 0, end: 2 },
        { start: 2, end: 3 },
        { start: 5, end: 8 },
      ]),
    ).toEqual([
      { start: 0, end: 3 },
      { start: 5, end: 8 },
    ]);
  });

  it("does not modify its input", () => {
    const ranges = [
      { start: 0, end: 2 },
      { start: 1, end: 4 },
    ];
    mergeRanges(ranges);
    expect(ranges).toEqual([
      { start: 0, end: 2 },
      { start: 1, end: 4 },
    ]);
  });
});
