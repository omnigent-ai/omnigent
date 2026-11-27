import { describe, expect, it } from "vitest";

import type { ContentMatch } from "@/lib/contentSearch";

import { textSearchItems, textSearchSummary } from "./textSearchItems";

const match = (
  path: string,
  line: number,
  text: string,
  start: number,
  end: number,
): ContentMatch => ({
  path,
  line,
  column: start + 1,
  text,
  match_start: start,
  match_end: end,
});

const plain = (query: string) => ({
  query,
  caseSensitive: false,
  regex: false,
  wholeWord: false,
});

describe("textSearchItems", () => {
  it("lists each line under its file, without indentation, with the match highlighted", () => {
    const [first, second] = textSearchItems(
      [
        match("src/a.ts", 3, "    const needle = 1;", 10, 16),
        match("src/a.ts", 9, "needle()", 0, 6),
      ],
      plain("needle"),
    );
    expect(first).toMatchObject({
      label: "const needle = 1;",
      detail: "3",
      group: "src/a.ts",
      highlights: { label: [{ start: 6, end: 12 }] },
      matchHighlight: "strong",
      value: { path: "src/a.ts", line: 3, column: 11, endColumn: 17 },
    });
    expect(first.label.slice(6, 12)).toBe("needle");
    expect(second.highlights?.label).toEqual([{ start: 0, end: 6 }]);
    expect(first.id).not.toBe(second.id);
  });

  it("highlights every occurrence on the line, shifted past the removed indentation", () => {
    const [item] = textSearchItems([match("a.ts", 1, "  foo(foo, Foo)", 2, 5)], plain("foo"));
    expect(item.highlights?.label).toEqual([
      { start: 0, end: 3 },
      { start: 4, end: 7 },
      { start: 9, end: 12 },
    ]);
  });

  it("drops a range inside the removed indentation", () => {
    const [item] = textSearchItems([match("a.ts", 1, "  x", 0, 1)], { ...plain(" "), regex: true });
    expect(item.highlights?.label).toEqual([]);
  });
});

describe("textSearchSummary", () => {
  it("counts results and files, and marks a search that stopped early", () => {
    const matches = [
      match("a.ts", 1, "x", 0, 1),
      match("a.ts", 2, "x", 0, 1),
      match("b.ts", 1, "x", 0, 1),
    ];
    expect(textSearchSummary({ matches, truncated: false })).toBe("3 results in 2 files");
    expect(textSearchSummary({ matches, truncated: true })).toBe("3+ results in 2 files");
    expect(textSearchSummary({ matches: matches.slice(0, 1), truncated: false })).toBe(
      "1 result in 1 file",
    );
  });
});
