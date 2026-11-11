import { describe, expect, it } from "vitest";

import {
  MatchClass,
  type MatchStrategy,
  compareCandidateMatches,
  foldText,
  matchCandidate,
  matchField,
  matchText,
  mergeRanges,
  occurrenceRanges,
  prepareFields,
  tokenizeQuery,
  wordCoalescedRanges,
} from "./match";

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

describe("matchField with words", () => {
  const words = (query: string, text: string) =>
    matchField(tokenizeQuery(query)[0], foldText(text), 0, "words");
  const highlighted = (query: string, text: string) => {
    const match = words(query, text);
    return match && match.ranges.map(({ start, end }) => text.slice(start, end));
  };

  it("matches pieces that each begin a word and continue inside it", () => {
    expect(highlighted("nt", "New tab")).toEqual(["N", "t"]);
    expect(highlighted("gostng", "Go to Settings")).toEqual(["Go", "S", "t", "ng"]);
    expect(highlighted("stng", "Settings")).toEqual(["S", "t", "ng"]);
    expect(highlighted("tsb", "Toggle sidebar")).toEqual(["T", "s", "b"]);
  });

  it("rejects a match that resumes in the middle of a later word", () => {
    expect(words("theme", "Show the comments column")).toBeNull();
    expect(words("goline", "Toggle conversations sidebar")).toBeNull();
    expect(words("ings", "Open settings")?.matchClass).toBe(MatchClass.substring);
  });

  it("scores fewer pieces, skipped words, and gaps as better", () => {
    const compact = words("nt", "New tab")!.penalty;
    const skipping = words("nt", "New chat tab")!.penalty;
    const gapped = words("stg", "Settings")!.penalty;
    const tight = words("stn", "Stand")!.penalty;
    expect(compact).toBeLessThan(skipping);
    expect(tight).toBeLessThan(gapped);
  });

  it("still finds a scattered subsequence with the default strategy", () => {
    expect(classOf("theme", "Show the comments column")).toBe(MatchClass.subsequence);
  });

  it("prefers a substring to a word-initial match", () => {
    expect(words("sett", "Open settings")?.matchClass).toBe(MatchClass.boundarySubstring);
  });
});

describe("matchCandidate", () => {
  const fields = prepareFields([
    { key: "label", text: "Fix login redirect", weight: 30 },
    { key: "description", text: "omnigent-ai/omnigent", weight: 20 },
    { text: "12101", weight: 10 },
  ]);

  it("requires every token to match some field and merges ranges per field", () => {
    const match = matchCandidate(tokenizeQuery("login omni"), fields);
    expect(match?.ranges.label).toEqual([{ start: 4, end: 9 }]);
    expect(match?.ranges.description).toEqual([{ start: 0, end: 4 }]);
    expect(matchCandidate(tokenizeQuery("login nothing"), fields)).toBeNull();
  });

  it("treats #123 as the bare number so PR numbers match hidden keywords", () => {
    const match = matchCandidate(tokenizeQuery("#12101"), fields);
    expect(match?.worstClass).toBe(MatchClass.exact);
    expect(match?.ranges).toEqual({});
  });

  it("ignores empty fields", () => {
    expect(prepareFields([{ text: "", weight: 1 }])).toEqual([]);
  });
});

describe("whole-query matches", () => {
  const match = (query: string, fields: ReturnType<typeof prepareFields>) =>
    matchCandidate(tokenizeQuery(query), fields)!;
  // Label, description, and keyword weights as a picker would set them.
  const candidate = (label: string, { description = "", keywords = [] as string[] } = {}) =>
    prepareFields([
      { key: "label", text: label, weight: 30 },
      { key: "description", text: description, weight: 20 },
      ...keywords.map((keyword) => ({ text: keyword, weight: 10 })),
    ]);

  it("ranks the label's phrase match first, then other fields' phrases, then separate words", () => {
    const ranked = {
      "label exact": candidate("Show Files"),
      "label prefix": candidate("Show files panel"),
      "label at a later word": candidate("Open show files"),
      "label mid-word": candidate("Reshow files"),
      "description phrase": candidate("Toggle panel", { description: "Show files here" }),
      "keyword phrase, exact": candidate("Toggle panel", { keywords: ["show files"] }),
      "keywords word by word, exact": candidate("Focus sidebar", { keywords: ["show", "files"] }),
      "label word by word": candidate("Files to show"),
    };
    const order = Object.entries(ranked)
      .map(([name, fields]) => ({ name, match: match("show files", fields) }))
      .reverse()
      .sort((first, second) => compareCandidateMatches(first.match, second.match))
      .map(({ name }) => name);
    expect(order).toEqual(Object.keys(ranked));
  });

  it("lets a synonym phrase win when no label holds the phrase", () => {
    const synonym = match("show files", candidate("Toggle panel", { keywords: ["show files"] }));
    const scattered = match("show files", candidate("Files: show all"));
    expect(compareCandidateMatches(synonym, scattered)).toBeLessThan(0);
    expect(synonym.ranges).toEqual({});
  });

  it("marks each word of the phrase where it best occurs, not the spaces between", () => {
    expect(match("show files", candidate("Show Files")).ranges.label).toEqual([
      { start: 0, end: 4 },
      { start: 5, end: 10 },
    ]);
    expect(match("show files", candidate("Reshow files")).ranges.label).toEqual([
      { start: 2, end: 6 },
      { start: 7, end: 12 },
    ]);
    // A later occurrence at a word beats an earlier one inside a word.
    const repeated = match("show files", candidate("Reshow files, then show files"));
    expect(repeated.worstClass).toBe(MatchClass.boundarySubstring);
    expect(repeated.ranges.label).toEqual([
      { start: 19, end: 23 },
      { start: 24, end: 29 },
    ]);
  });

  it("matches word by word when no field holds the words in order, and for one word", () => {
    expect(match("files show", candidate("Show Files")).wholeQuery).toBe(false);
    expect(match("show files", candidate("Show the files")).wholeQuery).toBe(false);
    // A single word is the whole query for every candidate, so it ranks as before.
    expect(match("files", candidate("Show Files")).wholeQuery).toBe(false);
  });
});

describe("tokenizeQuery", () => {
  it("splits on whitespace and folds each token", () => {
    expect(tokenizeQuery("  Café   Noir ")).toEqual([
      ["c", "a", "f", "e"],
      ["n", "o", "i", "r"],
    ]);
    expect(tokenizeQuery("   ")).toEqual([]);
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

describe("matchText", () => {
  it("returns one string's highlight ranges for a multi-word query, or null", () => {
    expect(matchText("cafe noir", "Le Cafe\u0301 noir")).toEqual([
      { start: 3, end: 8 },
      { start: 9, end: 13 },
    ]);
    expect(matchText("noir cafe", "Le Café noir")).toEqual([
      { start: 3, end: 7 },
      { start: 8, end: 12 },
    ]);
    expect(matchText("cafe tea", "Le Café noir")).toBeNull();
  });
});

describe("occurrenceRanges", () => {
  it("marks every occurrence of every token, folded like the matcher", () => {
    expect(occurrenceRanges("cafe", "Café or CAFE, then cafe")).toEqual([
      { start: 0, end: 4 },
      { start: 8, end: 12 },
      { start: 19, end: 23 },
    ]);
    expect(occurrenceRanges("deploy fail", "deploy failed; redeploy")).toEqual([
      { start: 0, end: 6 },
      { start: 7, end: 11 },
      { start: 17, end: 23 },
    ]);
  });

  it("merges overlapping tokens and steps past each occurrence", () => {
    expect(occurrenceRanges("ab bc", "abc")).toEqual([{ start: 0, end: 3 }]);
    expect(occurrenceRanges("aa", "aaaaa")).toEqual([{ start: 0, end: 4 }]);
  });

  it("reports UTF-16 offsets past astral and decomposed characters", () => {
    expect(occurrenceRanges("go", "🚀 go, 🚀 go")).toEqual([
      { start: 3, end: 5 },
      { start: 10, end: 12 },
    ]);
    expect(occurrenceRanges("e", "e\u0301e")).toEqual([{ start: 0, end: 3 }]);
  });

  it("returns nothing for a fuzzy-only match or an empty query", () => {
    expect(occurrenceRanges("opst", "Open settings")).toEqual([]);
    expect(occurrenceRanges("  ", "Open settings")).toEqual([]);
  });
});

describe("wordCoalescedRanges", () => {
  const words = (query: string, text: string) => {
    const ranges = matchField(tokenizeQuery(query)[0], foldText(text), 0, "words")!.ranges;
    return wordCoalescedRanges(text, ranges).map(({ start, end }) => text.slice(start, end));
  };

  it("marks a word whole when it holds several matched pieces", () => {
    expect(words("gostng", "Go to Settings")).toEqual(["Go", "Settings"]);
    expect(words("stng", "Settings")).toEqual(["Settings"]);
  });

  it("keeps a single run inside a word as typed", () => {
    expect(words("gose", "Go to Settings")).toEqual(["Go", "Se"]);
    expect(words("nt", "New tab")).toEqual(["N", "t"]);
  });

  it("leaves ranges that cross words or sit between them alone", () => {
    const text = "Show Files";
    expect(wordCoalescedRanges(text, [{ start: 2, end: 7 }])).toEqual([{ start: 2, end: 7 }]);
    expect(wordCoalescedRanges(text, [])).toEqual([]);
  });
});
