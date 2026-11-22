import { hydratePartialDiff, parsePatchFiles, parseDiffFromFile } from "@pierre/diffs";
import { describe, expect, it } from "vitest";
import { fullTextMatchesPatch } from "./fullTextMatchesPatch";

const patch = (...lines: string[]) =>
  parsePatchFiles(
    ["diff --git a/a.ts b/a.ts", "--- a/a.ts", "+++ b/a.ts", ...lines, ""].join("\n"),
  )[0]!.files[0]!;
const numbered = (count: number, change?: (line: string) => string) =>
  Array.from({ length: count }, (_, index) => `line ${index + 1}`)
    .map((line) => change?.(line) ?? line)
    .join("\n") + "\n";
const changed = patch("@@ -5,3 +5,3 @@", " line 5", "-line 6", "+line 6 changed", " line 7");
const before = numbered(20);
const after = numbered(20, (line) => (line === "line 6" ? "line 6 changed" : line));
const text = (contents: string) => ({ name: "a.ts", contents });

describe("fullTextMatchesPatch", () => {
  it("takes the text the patch was made from", () => {
    expect(fullTextMatchesPatch(changed, { oldFile: text(before), newFile: text(after) })).toBe(
      true,
    );
  });

  it("refuses a side that's missing or shorter than the patch", () => {
    // Filled in anyway, these rows would have no line on either side.
    const files = { oldFile: text(numbered(2)), newFile: text(numbered(2)) };
    const hydrated = hydratePartialDiff("clone", changed, files);
    expect(hydrated.deletionLines).toHaveLength(2);
    expect(fullTextMatchesPatch(changed, files)).toBe(false);
    expect(fullTextMatchesPatch(changed, { oldFile: text(before), newFile: text("") })).toBe(false);
  });

  it("refuses text from another revision with the same length", () => {
    const moved = numbered(20, (line) => (line === "line 5" ? "line 5 moved" : line));
    expect(fullTextMatchesPatch(changed, { oldFile: text(moved), newFile: text(after) })).toBe(
      false,
    );
  });

  it("checks every hunk, added and deleted lines included", () => {
    const twoHunks = patch(
      "@@ -1,2 +1,2 @@",
      "-line 1",
      "+line one",
      " line 2",
      "@@ -18,3 +18,4 @@",
      " line 18",
      "+line 18.5",
      " line 19",
      " line 20",
    );
    const newer = numbered(20, (line) =>
      line === "line 1" ? "line one" : line === "line 18" ? "line 18\nline 18.5" : line,
    );
    expect(fullTextMatchesPatch(twoHunks, { oldFile: text(before), newFile: text(newer) })).toBe(
      true,
    );
    const lostAddition = numbered(20, (line) => (line === "line 1" ? "line one" : line));
    expect(
      fullTextMatchesPatch(twoHunks, { oldFile: text(before), newFile: text(lostAddition) }),
    ).toBe(false);
  });

  it("takes a last line without a newline, and a file that adds every line", () => {
    const noNewline = patch(
      "@@ -1,2 +1,2 @@",
      " line 1",
      "-line 2",
      "\\ No newline at end of file",
      "+line two",
      "\\ No newline at end of file",
    );
    expect(
      fullTextMatchesPatch(noNewline, {
        oldFile: text("line 1\nline 2"),
        newFile: text("line 1\nline two"),
      }),
    ).toBe(true);
    const added = patch("@@ -0,0 +1,2 @@", "+one", "+two");
    expect(fullTextMatchesPatch(added, { oldFile: text(""), newFile: text("one\ntwo\n") })).toBe(
      true,
    );
  });

  it("has nothing to check on a diff of whole files", () => {
    const whole = parseDiffFromFile(text("a\n"), text("b\n"));
    expect(fullTextMatchesPatch(whole, { oldFile: text(""), newFile: text("") })).toBe(true);
  });
});
