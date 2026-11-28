import { describe, expect, it } from "vitest";

import { compareFile, comparison } from "./stackDiffFixtures";
import {
  STACK_DIFF_NOT_AVAILABLE,
  stackCompareFileDiff,
  stackCompareReview,
} from "./stackCompareReview";

describe("stackCompareReview", () => {
  it("shows a proven patch as the file's diff, under its own path", () => {
    const review = stackCompareReview(comparison());
    expect(review.files).toEqual([
      expect.objectContaining({
        path: "src/app.ts",
        change: "modified",
        additions: 1,
        deletions: 1,
      }),
    ]);
    const content = review.contents.get("src/app.ts");
    expect(content?.status).toBe("ready");
    expect(content?.status === "ready" && content.fileDiff.hunks).toHaveLength(1);
  });

  it("lists a file it can't show a diff of, with its status, pointing to All changes", () => {
    const review = stackCompareReview(
      comparison({
        files: [
          compareFile({ path: "big.ts", patch: null, patch_state: "incomplete" }),
          compareFile({
            path: "gone.ts",
            status: "removed",
            patch: null,
            patch_state: "unavailable",
          }),
          compareFile({
            path: "new/name.ts",
            previous_path: "old/name.ts",
            status: "renamed",
            additions: 0,
            deletions: 0,
            patch: null,
            patch_state: "unavailable",
          }),
        ],
      }),
    );
    expect(review.files.map((file) => [file.path, file.change, file.notes])).toEqual([
      ["big.ts", "modified", undefined],
      ["gone.ts", "deleted", undefined],
      ["new/name.ts", "modified", ["Renamed from old/name.ts"]],
    ]);
    for (const path of ["big.ts", "gone.ts", "new/name.ts"]) {
      expect(review.contents.get(path)).toEqual({
        status: "unavailable",
        message: STACK_DIFF_NOT_AVAILABLE,
      });
    }
    expect(review.previousPaths.get("new/name.ts")).toBe("old/name.ts");
  });

  it("keeps an added, removed or renamed file's kind of change in its diff", () => {
    expect(
      stackCompareFileDiff(
        compareFile({ status: "added", additions: 1, deletions: 0, patch: "@@ -0,0 +1 @@\n+hi" }),
      )?.type,
    ).toBe("new");
    expect(
      stackCompareFileDiff(
        compareFile({ status: "removed", additions: 0, deletions: 1, patch: "@@ -1 +0,0 @@\n-hi" }),
      )?.type,
    ).toBe("deleted");
    const renamed = stackCompareFileDiff(
      compareFile({ path: "new/name.ts", previous_path: "old/name.ts", status: "renamed" }),
    );
    expect([renamed?.type, renamed?.name, renamed?.prevName]).toEqual([
      "rename-changed",
      "new/name.ts",
      "old/name.ts",
    ]);
  });

  it("keeps paths with spaces, quotes or other scripts exactly as the shell named them", () => {
    for (const path of ['docs/my "notes".md', "src/café.ts", "a b/c\\d.ts"]) {
      expect(stackCompareFileDiff(compareFile({ path }))?.name).toBe(path);
    }
  });

  it("shows no diff for a path that would break the patch's header", () => {
    expect(stackCompareFileDiff(compareFile({ path: "odd\nname.ts" }))).toBeNull();
  });
});
