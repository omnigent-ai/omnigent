import { parsePatchFiles } from "@pierre/diffs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { reviewItemId } from "./diffReviewModel";
import { computeFileDiff } from "./fileDiffComputation";
import {
  currentLineForOldLine,
  fileAtViewportTop,
  hunksHaveLine,
  rangeFits,
  readScrollAnchor,
} from "./reviewNavigation";

describe("fileAtViewportTop", () => {
  it("picks the topmost file still showing at the viewport's top", () => {
    const items = [
      { path: "c.ts", top: 600, bottom: 900 },
      { path: "a.ts", top: -400, bottom: 100 },
      { path: "b.ts", top: 100, bottom: 600 },
    ];
    expect(fileAtViewportTop(0, items)).toBe("a.ts");
    expect(fileAtViewportTop(100, items)).toBe("b.ts");
    expect(fileAtViewportTop(1000, items)).toBeUndefined();
  });
});

describe("rangeFits", () => {
  it("compares the range's height with the viewport", () => {
    expect(rangeFits(10, 19, 20, 200)).toBe(true);
    expect(rangeFits(10, 20, 20, 200)).toBe(false);
  });
});

describe("readScrollAnchor", () => {
  afterEach(() => vi.restoreAllMocks());

  function item(path: string, top: number, bottom: number, rowsHtml = "") {
    const element = document.createElement("div");
    element.attachShadow({ mode: "open" }).innerHTML = `<div data-content>${rowsHtml}</div>`;
    vi.spyOn(element, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ y: top, height: bottom - top }),
    );
    return { id: reviewItemId(path), element };
  }

  it("anchors to the top file's first current-version line on screen", () => {
    const top = item("a.ts", -500, 50, '<div data-line="150" data-line-type="context">x</div>');
    vi.spyOn(
      top.element.shadowRoot!.querySelector("[data-line]")!,
      "getBoundingClientRect",
    ).mockReturnValue(DOMRect.fromRect({ y: -8, height: 20 }));
    expect(readScrollAnchor(0, [item("b.ts", 50, 400), top])).toEqual({
      path: "a.ts",
      line: 150,
      offset: -8,
    });
  });

  it("anchors to the file's top when no line of it is rendered on screen", () => {
    expect(readScrollAnchor(0, [item("a.ts", -30, 300)])).toEqual({ path: "a.ts", offset: -30 });
    expect(readScrollAnchor(0, [])).toBeUndefined();
  });
});

describe("currentLineForOldLine", () => {
  const before = `${Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join("\n")}\n`;
  // Old line 2 is deleted and two lines are added after old line 30.
  const after = before.replace("line 2\n", "").replace("line 30\n", "line 30\nnew a\nnew b\n");
  const fileDiff = computeFileDiff({
    path: "a.ts",
    before,
    after,
    ignoreWhitespace: false,
    revision: "1",
  });

  it("maps unchanged old lines before, between, and after the hunks", () => {
    expect(currentLineForOldLine(fileDiff, 1)).toBe(1);
    expect(currentLineForOldLine(fileDiff, 3)).toBe(2);
    expect(currentLineForOldLine(fileDiff, 15)).toBe(14);
    expect(currentLineForOldLine(fileDiff, 31)).toBe(32);
    expect(currentLineForOldLine(fileDiff, 40)).toBe(41);
  });

  it("has no current line for a deleted line", () => {
    expect(currentLineForOldLine(fileDiff, 2)).toBeUndefined();
  });

  const zeroContext = (hunk: string, body: string[]) =>
    parsePatchFiles(
      ["diff --git a/a.ts b/a.ts", "--- a/a.ts", "+++ b/a.ts", hunk, ...body, ""].join("\n"),
    )[0]!.files[0]!;

  it("treats a zero-context insertion's old start as the line before it", () => {
    const inserted = zeroContext("@@ -5,0 +6,1 @@", ["+new"]);
    expect([4, 5, 6].map((line) => currentLineForOldLine(inserted, line))).toEqual([4, 5, 7]);
  });

  it("treats a zero-context deletion's new start as the line before it", () => {
    const deleted = zeroContext("@@ -5,1 +4,0 @@", ["-gone"]);
    expect(currentLineForOldLine(deleted, 4)).toBe(4);
    expect(currentLineForOldLine(deleted, 5)).toBeUndefined();
    expect(currentLineForOldLine(deleted, 6)).toBe(5);
  });
});

describe("hunksHaveLine", () => {
  const patch = parsePatchFiles(
    [
      "diff --git a/a.ts b/a.ts",
      "--- a/a.ts",
      "+++ b/a.ts",
      "@@ -10,3 +9,2 @@",
      " keep",
      "-gone",
      " last",
      "",
    ].join("\n"),
  )[0]!.files[0]!;

  it("knows which lines of each side a patch carries", () => {
    // New lines 9–10 and old lines 10–12.
    expect([8, 9, 10, 11].map((line) => hunksHaveLine(patch, line, "additions"))).toEqual([
      false,
      true,
      true,
      false,
    ]);
    expect([9, 10, 12, 13].map((line) => hunksHaveLine(patch, line, "deletions"))).toEqual([
      false,
      true,
      true,
      false,
    ]);
  });
});
