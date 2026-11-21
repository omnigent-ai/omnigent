import { hydratePartialDiff, parsePatchFiles } from "@pierre/diffs";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { reviewFile } from "./diffReviewModel";
import { computeFileDiff } from "./fileDiffComputation";
import type { DiffReviewContent } from "./reviewItems";
import {
  clearViewed,
  diffFingerprint,
  markViewed,
  resetUnsavedViewedMarks,
  useViewedMarks,
  viewedBatch,
  viewedFingerprint,
  viewedMarkCovers,
  viewedUnavailableReason,
} from "./viewedFiles";

function diff(before: string, after: string, revision = "1") {
  return computeFileDiff({ path: "a.ts", before, after, ignoreWhitespace: false, revision });
}

afterEach(() => {
  window.localStorage.clear();
  resetUnsavedViewedMarks();
});

describe("diffFingerprint", () => {
  it("names the changes, not the revision or the unchanged lines' place in a reload", () => {
    const before = "one\ntwo\nthree\n";
    expect(diffFingerprint(diff(before, "one\nTWO\nthree\n", "1"))).toBe(
      diffFingerprint(diff(before, "one\nTWO\nthree\n", "2")),
    );
  });

  it("stays the same once a patch's full text loads, so expanding context keeps the mark", () => {
    const before = Array.from({ length: 20 }, (_, index) => `line ${index}`);
    const after = before.map((line, index) => (index === 10 ? "changed" : line));
    const patch = [
      "diff --git a/a.ts b/a.ts",
      "--- a/a.ts",
      "+++ b/a.ts",
      "@@ -8,7 +8,7 @@",
      ...before.slice(7, 10).map((line) => ` ${line}`),
      "-line 10",
      "+changed",
      ...before.slice(11, 14).map((line) => ` ${line}`),
      "",
    ].join("\n");
    const partial = parsePatchFiles(patch)[0]!.files[0]!;
    expect(partial.isPartial).toBe(true);
    const full = hydratePartialDiff("clone", partial, {
      oldFile: { name: "a.ts", contents: before.join("\n") + "\n" },
      newFile: { name: "a.ts", contents: after.join("\n") + "\n" },
    });
    expect(full.isPartial).toBe(false);
    expect(diffFingerprint(full)).toBe(diffFingerprint(partial));
  });

  it("changes when the changed lines or where they sit change", () => {
    const before = "one\ntwo\nthree\n";
    const marked = diffFingerprint(diff(before, "one\nTWO\nthree\n"));
    expect(diffFingerprint(diff(before, "one\nTwo!\nthree\n"))).not.toBe(marked);
    expect(diffFingerprint(diff(before, "ONE\ntwo\nthree\n"))).not.toBe(marked);
  });

  it("tells apart the same edit made to different copies of a repeated line", () => {
    const before = "start\nrepeat\nmiddle\nrepeat\nend\n";
    const first = diff(before, "start\nREPEAT\nmiddle\nrepeat\nend\n");
    const second = diff(before, "start\nrepeat\nmiddle\nREPEAT\nend\n");
    expect(diffFingerprint(first)).not.toBe(diffFingerprint(second));
  });

  it("fingerprints a very large added file", () => {
    const added = computeFileDiff({
      path: "big.txt",
      before: null,
      after: "a\n".repeat(140_000),
      ignoreWhitespace: false,
      revision: "1",
    });
    expect(diffFingerprint(added)).toMatch(/^[0-9a-z]+$/);
  });
});

describe("viewedFingerprint and viewedMarkCovers", () => {
  const file = reviewFile("a.ts", "modified", { additions: 1, deletions: 1 });
  const ready: DiffReviewContent = { status: "ready", fileDiff: diff("a\n", "b\n") };

  it("has no fingerprint while the diff loads", () => {
    expect(viewedFingerprint(file, undefined)).toBeNull();
    expect(viewedFingerprint(file, { status: "loading" })).toBeNull();
    expect(viewedFingerprint(file, ready)).toMatch(/^modified:/);
  });

  it("records the source's content identity for a file it can't diff, and nothing without one", () => {
    const unavailable = (revision?: string): DiffReviewContent => ({
      status: "unavailable",
      message: "Binary file",
      revision,
    });
    const marked = viewedFingerprint(file, unavailable("1111111..2222222"));
    expect(marked).toBe("modified:source:1111111..2222222");
    // Same line counts, different content: a new revision.
    expect(
      viewedMarkCovers(marked!, viewedFingerprint(file, unavailable("1111111..3333333"))),
    ).toBe(false);
    expect(viewedFingerprint(file, unavailable())).toBeNull();
    expect(viewedFingerprint(file, { status: "failed", message: "boom" })).toBeNull();
  });

  it("explains why a file can't be marked yet", () => {
    expect(viewedUnavailableReason(file, ready)).toBeNull();
    expect(viewedUnavailableReason(file, { status: "loading" })).toMatch(/once its diff loads/);
    expect(viewedUnavailableReason(file, { status: "failed", message: "boom" })).toMatch(
      /once its diff loads/,
    );
    expect(viewedUnavailableReason(file, { status: "unavailable", message: "gone" })).toMatch(
      /can't be compared/,
    );
  });

  it("covers only the marked revision, keeping the fold while the file loads", () => {
    const current = viewedFingerprint(file, ready);
    expect(viewedMarkCovers(current!, current)).toBe(true);
    expect(viewedMarkCovers("modified:other", current)).toBe(false);
    expect(viewedMarkCovers("", current)).toBe(false);
    expect(viewedMarkCovers("modified:other", null)).toBe(true);
    expect(viewedMarkCovers(undefined, current)).toBe(false);
  });
});

describe("viewedBatch", () => {
  const ready = (path: string): DiffReviewContent => ({
    status: "ready",
    fileDiff: computeFileDiff({
      path,
      before: "a\n",
      after: "b\n",
      ignoreWhitespace: false,
      revision: "1",
    }),
  });
  const counts = { additions: 1, deletions: 1 };

  it("marks files whose changes are known, clears viewed ones, and skips the uncomparable", () => {
    const files = [
      reviewFile("viewed.ts", "modified", counts),
      reviewFile("ready.ts", "modified", counts),
      reviewFile("gone.ts", "modified", counts),
      reviewFile("failed.ts", "modified", counts),
    ];
    const contents = new Map<string, DiffReviewContent>([
      ["viewed.ts", ready("viewed.ts")],
      ["ready.ts", ready("ready.ts")],
      ["gone.ts", { status: "unavailable", message: "gone" }],
      ["failed.ts", { status: "failed", message: "boom" }],
    ]);
    const batch = viewedBatch(files, new Set(["viewed.ts"]), contents);
    expect(batch.toMark).toEqual([
      { path: "ready.ts", fingerprint: viewedFingerprint(files[1]!, contents.get("ready.ts")) },
    ]);
    expect(batch.toClear).toEqual(["viewed.ts"]);
    expect(batch.loading).toBe(false);
  });

  it("waits for a loading text file, but not for a binary one", () => {
    const text = reviewFile("a.ts", "modified", counts);
    const loading = new Map<string, DiffReviewContent>([["a.ts", { status: "loading" }]]);
    expect(viewedBatch([text], new Set(), loading).loading).toBe(true);
    // A viewed file needs no load to clear.
    expect(viewedBatch([text], new Set(["a.ts"]), loading)).toEqual({
      toMark: [],
      toClear: ["a.ts"],
      loading: false,
    });
    const binary = { ...reviewFile("logo.png", "modified", counts), isBinary: true };
    const binaryLoading = new Map<string, DiffReviewContent>([["logo.png", { status: "loading" }]]);
    expect(viewedBatch([binary], new Set(), binaryLoading).loading).toBe(false);
  });
});

describe("viewed marks", () => {
  it("are remembered per review, and cleared", () => {
    markViewed("changes:one", "a.ts", "modified:x");
    markViewed("changes:one", "b.ts", "added:y");
    const { result, rerender } = renderHook(({ scope }) => useViewedMarks(scope), {
      initialProps: { scope: "changes:one" },
    });
    expect([...result.current]).toEqual([
      ["a.ts", "modified:x"],
      ["b.ts", "added:y"],
    ]);
    rerender({ scope: "pull-request:two" });
    expect(result.current.size).toBe(0);
    clearViewed("changes:one", "a.ts");
    rerender({ scope: "changes:one" });
    expect([...result.current.keys()]).toEqual(["b.ts"]);
  });
});
