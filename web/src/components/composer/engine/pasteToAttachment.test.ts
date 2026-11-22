import { describe, expect, it } from "vitest";
import {
  exceedsPasteThreshold,
  insertAtOffset,
  lineCount,
  mapOffsetThroughEdit,
  mapOffsetThroughReplace,
  pastedPlainText,
  pastedTextFile,
} from "./pasteToAttachment";

const lines = (count: number) => Array.from({ length: count }, (_, index) => `line ${index}`);

describe("the large-paste threshold", () => {
  it.each([
    [39, false],
    [40, true],
  ])("%i lines attach: %s", (count, attaches) => {
    expect(exceedsPasteThreshold(lines(count).join("\n"))).toBe(attaches);
  });

  it.each([
    [3999, false],
    [4000, true],
  ])("%i characters on one line attach: %s", (length, attaches) => {
    expect(exceedsPasteThreshold("x".repeat(length))).toBe(attaches);
  });

  it("counts lines the way the draft shows them", () => {
    expect(lineCount("")).toBe(0);
    expect(lineCount("one")).toBe(1);
    expect(lineCount("one\ntwo\n")).toBe(3);
  });

  it("reads plain text with Windows and classic Mac line endings normalized", () => {
    const data = { getData: (type: string) => (type === "text/plain" ? "a\r\nb\rc" : "<b>a</b>") };
    expect(pastedPlainText(data)).toBe("a\nb\nc");
  });
});

describe("the pasted text attachment", () => {
  it("is a plain-text file numbered by its paste", async () => {
    const file = pastedTextFile("hello", 2);
    expect(file.name).toBe("pasted-text-2.txt");
    expect(file.type).toBe("text/plain");
    expect(await file.text()).toBe("hello");
  });
});

describe("following a remembered offset through edits", () => {
  it.each([
    ["typing before it", "abc|def", "Xabcdef", 4],
    ["typing after it", "abc|def", "abcdefX", 3],
    ["typing at it", "abc|def", "abcXdef", 3],
    ["deleting before it", "abc|def", "bcdef", 2],
    ["deleting across it", "ab|cdef", "aef", 1],
    ["replacing the whole draft", "abc|def", "", 0],
  ])("keeps its place when %s", (_name, marked, after, expected) => {
    // Each case marks the offset with exactly one "|".
    const offset = marked.indexOf("|");
    const before = marked.slice(0, offset) + marked.slice(offset + 1);
    expect(mapOffsetThroughEdit(offset, before, after)).toBe(expected);
  });

  it("inserts at a clamped offset and never deletes text", () => {
    expect(insertAtOffset("draft", 99, " pasted")).toBe("draft pasted");
    expect(insertAtOffset("draft", -3, "pasted ")).toBe("pasted draft");
    expect(insertAtOffset("ab", 1, "X")).toBe("aXb");
  });
});

describe("mapOffsetThroughReplace", () => {
  it("keeps offsets before the span, shifts those after it, and pulls those inside to its start", () => {
    expect(mapOffsetThroughReplace(0, 0, 1, 0)).toBe(0);
    expect(mapOffsetThroughReplace(3, 0, 1, 0)).toBe(2);
    expect(mapOffsetThroughReplace(1, 0, 1, 0)).toBe(0);
    expect(mapOffsetThroughReplace(2, 1, 4, 5)).toBe(1);
    expect(mapOffsetThroughReplace(4, 1, 4, 5)).toBe(6);
  });
});
