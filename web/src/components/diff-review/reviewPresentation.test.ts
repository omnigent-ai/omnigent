import { describe, expect, it } from "vitest";

import { reviewFile, type DiffReviewFile } from "./diffReviewModel";
import { computeFileDiff } from "./fileDiffComputation";
import type { DiffReviewContent } from "./reviewItems";
import { onlyWhitespaceChanged } from "./reviewPresentation";

function readyWith(path: string, before: string, after: string): DiffReviewContent {
  return {
    status: "ready",
    fileDiff: computeFileDiff({ path, before, after, ignoreWhitespace: true, revision: "1" }),
  };
}

const SPACED = reviewFile("a.ts", "modified", { additions: 1, deletions: 1 });
const SPACED_TOO = reviewFile("b.ts", "modified", { additions: 2, deletions: 2 });
// A re-indent: ignoring whitespace leaves no change.
const whitespaceOnly = (path: string) => readyWith(path, "let a = 1;\n", "  let a = 1;\n");
const realChange = (path: string) => readyWith(path, "let a = 1;\n", "let a = 2;\n");

function contentsOf(entries: [DiffReviewFile, DiffReviewContent][]) {
  return new Map(entries.map(([file, content]) => [file.path, content]));
}

describe("onlyWhitespaceChanged", () => {
  it("is true once every file has loaded and ignoring whitespace left none of their changes", () => {
    const contents = contentsOf([
      [SPACED, whitespaceOnly(SPACED.path)],
      [SPACED_TOO, whitespaceOnly(SPACED_TOO.path)],
    ]);
    expect(onlyWhitespaceChanged([SPACED, SPACED_TOO], contents, true)).toBe(true);
    expect(onlyWhitespaceChanged([SPACED, SPACED_TOO], contents, false)).toBe(false);
  });

  it("is false with no files, or while one still has real changes", () => {
    expect(onlyWhitespaceChanged([], new Map(), true)).toBe(false);
    const contents = contentsOf([
      [SPACED, whitespaceOnly(SPACED.path)],
      [SPACED_TOO, realChange(SPACED_TOO.path)],
    ]);
    expect(onlyWhitespaceChanged([SPACED, SPACED_TOO], contents, true)).toBe(false);
  });

  it("waits for every file: a loading, failed, unavailable or missing one isn't known yet", () => {
    for (const second of [
      { status: "loading" },
      { status: "failed", message: "offline" },
      { status: "unavailable", message: "Original content unavailable" },
      undefined,
    ] as (DiffReviewContent | undefined)[]) {
      const contents = new Map<string, DiffReviewContent>([
        [SPACED.path, whitespaceOnly(SPACED.path)],
      ]);
      if (second) contents.set(SPACED_TOO.path, second);
      expect(onlyWhitespaceChanged([SPACED, SPACED_TOO], contents, true)).toBe(false);
    }
  });

  it("never hides a change that isn't in the lines: binary, submodule notes, or metadata only", () => {
    const binary = reviewFile("logo.png", "modified", {
      additions: 1,
      deletions: 1,
      isBinary: true,
    });
    const submodule = {
      ...reviewFile("vendor/lib", "modified", { additions: 1, deletions: 1 }),
      notes: ["Submodule commit changed"],
    };
    const modeOnly = reviewFile("run.sh", "modified", { additions: 0, deletions: 0 });
    for (const file of [binary, submodule, modeOnly]) {
      const contents = contentsOf([
        [SPACED, whitespaceOnly(SPACED.path)],
        [file, readyWith(file.path, "x\n", "x\n")],
      ]);
      expect(onlyWhitespaceChanged([SPACED, file], contents, true)).toBe(false);
    }
  });
});
