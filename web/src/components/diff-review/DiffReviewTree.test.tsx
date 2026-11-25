import { describe, expect, it } from "vitest";
import { orderTreePaths } from "@/components/file-tree/vendored";
import { reviewFile, treeOrderedFiles } from "./diffReviewModel";
import {
  givenOrder,
  treeGitStatus,
  treeGitStatusEntries,
  treeRowContentKey,
  treeRowDecoration,
} from "./DiffReviewTree";

describe("treeGitStatus", () => {
  it("marks added and deleted files, and leaves modified ones plain", () => {
    expect(treeGitStatus("added")).toBe("added");
    expect(treeGitStatus("deleted")).toBe("deleted");
    expect(treeGitStatus("modified")).toBeNull();
  });
});

describe("treeGitStatusEntries", () => {
  it("lists every file with its letter's status in the reference appearance", () => {
    expect(
      treeGitStatusEntries(
        [
          reviewFile("a.ts", "added"),
          reviewFile("b.ts", "modified"),
          reviewFile("c.ts", "deleted"),
        ],
        "reference",
      ),
    ).toEqual([
      { path: "a.ts", status: "added" },
      { path: "b.ts", status: "modified" },
      { path: "c.ts", status: "deleted" },
    ]);
  });

  it("lists only added and deleted files", () => {
    expect(
      treeGitStatusEntries([
        reviewFile("new.ts", "added"),
        reviewFile("same.ts", "modified"),
        reviewFile("gone.ts", "deleted"),
      ]),
    ).toEqual([
      { path: "new.ts", status: "added" },
      { path: "gone.ts", status: "deleted" },
    ]);
  });
});

describe("treeRowDecoration", () => {
  it("shows added and deleted line counts in their colors", () => {
    expect(
      treeRowDecoration(reviewFile("a.ts", "modified", { additions: 3, deletions: 1 })),
    ).toEqual({
      text: "+3 −1",
      title: "3 added, 1 deleted",
      parts: [
        { text: "+3", color: "var(--success)" },
        { text: "−1", color: "var(--destructive)" },
      ],
    });
  });

  it("leaves out a side with no changed lines", () => {
    expect(treeRowDecoration(reviewFile("a.ts", "added", { additions: 2, deletions: 0 }))).toEqual(
      expect.objectContaining({ text: "+2", title: "2 added" }),
    );
  });

  it("marks a file the reader owns before its counts, even with none", () => {
    const owned = {
      ...reviewFile("a.ts", "modified", { additions: 3, deletions: 0 }),
      ownedByViewer: true,
    };
    expect(treeRowDecoration(owned)).toEqual({
      text: "● +3",
      title: "Owned by you, 3 added",
      parts: [
        { text: "●", color: "var(--primary)" },
        { text: "+3", color: "var(--success)" },
      ],
    });
    expect(treeRowDecoration({ ...reviewFile("a.png", "modified"), ownedByViewer: true })).toEqual(
      expect.objectContaining({ text: "●", title: "Owned by you" }),
    );
  });

  it("shows nothing while the source hasn't counted the lines", () => {
    expect(treeRowDecoration(reviewFile("a.png", "modified"))).toBeNull();
  });
});

describe("treeRowContentKey", () => {
  it("changes when a file's counts or status change, not otherwise", () => {
    const before = [reviewFile("a.ts", "modified", { additions: 1, deletions: 0 })];
    expect(treeRowContentKey(before)).toBe(
      treeRowContentKey([reviewFile("a.ts", "modified", { additions: 1, deletions: 0 })]),
    );
    expect(treeRowContentKey(before)).not.toBe(
      treeRowContentKey([reviewFile("a.ts", "modified", { additions: 2, deletions: 0 })]),
    );
    expect(treeRowContentKey(before)).not.toBe(
      treeRowContentKey([reviewFile("a.ts", "added", { additions: 1, deletions: 0 })]),
    );
    expect(treeRowContentKey(before)).not.toBe(
      treeRowContentKey([{ ...before[0], ownedByViewer: true }]),
    );
  });
});

describe("givenOrder", () => {
  it("builds the tree in the order the review gives, so the tree and the diff read alike", () => {
    const files = [
      reviewFile("README.md", "modified", { additions: 3 }),
      reviewFile("src/app/main.py", "modified", { additions: 1 }),
      reviewFile("src/app/session_list.py", "modified", { additions: 14, deletions: 6 }),
      reviewFile("docs/guide.md", "added", { additions: 2 }),
    ];
    const bySize = treeOrderedFiles(files, "size").map((file) => file.path);
    // Largest first across folders: src before docs, and the root file between them.
    expect(bySize).toEqual([
      "src/app/session_list.py",
      "src/app/main.py",
      "README.md",
      "docs/guide.md",
    ]);
    const tree = (paths: string[]) =>
      orderTreePaths(paths, { flattenEmptyDirectories: true, sort: givenOrder(paths) });
    expect(tree(bySize)).toEqual(bySize);
    const byPath = treeOrderedFiles(files).map((file) => file.path);
    expect(tree(byPath)).toEqual(byPath);
  });

  it("orders a path the review didn't name by the tree's default", () => {
    const compare = givenOrder(["b.ts"]);
    const entry = (path: string) => ({
      basename: path,
      depth: 1,
      isDirectory: false,
      path,
      segments: [path],
    });
    expect(compare(entry("a.ts"), entry("b.ts"))).toBeLessThan(0);
    expect(compare(entry("b.ts"), entry("a.ts"))).toBeGreaterThan(0);
  });
});
