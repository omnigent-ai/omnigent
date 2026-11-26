import { describe, expect, it } from "vitest";
import { lineSegments, offsetAt, positionAt, visibleLength } from "./textAnchors";

const lines = ["one\n", "two\r\n", "", "three"].filter((line) => line !== "");
const text = lines.join("");

describe("textAnchors", () => {
  it("maps offsets to lines and columns and back", () => {
    for (const [offset, position] of [
      [0, { line: 1, column: 0 }],
      [2, { line: 1, column: 2 }],
      [4, { line: 2, column: 0 }],
      [9, { line: 3, column: 0 }],
      [14, { line: 3, column: 5 }],
    ] as const) {
      expect(positionAt(lines, offset)).toEqual(position);
      expect(offsetAt(lines, position)).toBe(offset);
    }
    expect(text.slice(offsetAt(lines, { line: 3, column: 0 }))).toBe("three");
  });

  it("puts offsets inside a line ending at the end of that line", () => {
    expect(positionAt(lines, 3)).toEqual({ line: 1, column: 3 });
    expect(positionAt(lines, 8)).toEqual({ line: 2, column: 3 });
  });

  it("clamps columns and rejects positions outside the text", () => {
    expect(offsetAt(lines, { line: 2, column: 99 })).toBe(7);
    expect(offsetAt(lines, { line: 9, column: 0 })).toBeUndefined();
    expect(positionAt(lines, -1)).toBeUndefined();
    expect(positionAt(lines, text.length + 1)).toBeUndefined();
    expect(positionAt([], 0)).toBeUndefined();
  });

  it("splits a range into the visible part of each line", () => {
    expect(lineSegments(lines, 1, 11)).toEqual([
      { line: 1, from: 1, to: 3 },
      { line: 2, from: 0, to: 3 },
      { line: 3, from: 0, to: 2 },
    ]);
    // A range that covers only a line ending has nothing to show.
    expect(lineSegments(lines, 3, 4)).toEqual([]);
    expect(lineSegments(lines, 5, 5)).toEqual([]);
    expect(lineSegments(lines, 10, 999)).toEqual([{ line: 3, from: 1, to: 5 }]);
  });

  it("measures text without its line ending", () => {
    expect(visibleLength("a\r\n")).toBe(1);
    expect(visibleLength("ab\n")).toBe(2);
    expect(visibleLength("abc")).toBe(3);
  });
});
