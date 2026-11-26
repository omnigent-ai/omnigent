import { FileTreeModel } from "@/components/file-tree/vendored";
import { describe, expect, it } from "vitest";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { type FolderListing, syncFolderListings, visibleTreeRow } from "./folderTreeSync";

function folder(path: string, leafDirectoryPath?: string): WorkspaceFile {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    type: "directory",
    bytes: null,
    modified_at: null,
    ...(leafDirectoryPath ? { leaf_directory_path: leafDirectoryPath } : {}),
  } as WorkspaceFile;
}

function file(path: string): WorkspaceFile {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    type: "file",
    bytes: null,
    modified_at: null,
  } as WorkspaceFile;
}

/** A real tree built from first listings, refreshed the way the folder tree refreshes it. */
function liveTree(listings: FolderListing[]) {
  const first = syncFolderListings(new Set(), listings);
  const model = new FileTreeModel({
    paths: [...first.present],
    flattenEmptyDirectories: true,
    initialExpansion: "open",
  });
  let present = first.present;
  return {
    model,
    first,
    refresh(next: FolderListing[]) {
      const sync = syncFolderListings(present, next);
      model.batch(sync.operations);
      present = sync.present;
      return sync;
    },
    holds: (path: string) => model.getItem(path) !== null,
  };
}

// `pkg` holds only `inner`, so the root listing names the chain down to it.
const ROOT_WITH_CHAIN: FolderListing = { folder: "", entries: [folder("pkg", "pkg/inner")] };
const INNER: FolderListing = { folder: "pkg/inner", entries: [file("pkg/inner/a.ts")] };

describe("syncFolderListings against a real tree", () => {
  it("keeps a chain's folders when the hint goes away, and lists the folder for real", () => {
    const tree = liveTree([ROOT_WITH_CHAIN, INNER]);
    expect(tree.first.loadedFolders).toContain("pkg");

    // `pkg` gained a sibling of `inner`, so the root no longer describes a chain.
    const contracted = tree.refresh([{ folder: "", entries: [folder("pkg")] }, INNER]);

    expect(contracted.operations).toEqual([]);
    expect(tree.holds("pkg/inner/")).toBe(true);
    expect(tree.holds("pkg/inner/a.ts")).toBe(true);
    // No longer known to be complete, so the tree lists it and finds the new file.
    expect(contracted.loadedFolders).not.toContain("pkg");
    const relisted = tree.refresh([
      { folder: "", entries: [folder("pkg")] },
      { folder: "pkg", entries: [folder("pkg/inner"), file("pkg/b.ts")] },
      INNER,
    ]);
    expect(relisted.operations).toEqual([{ type: "add", path: "pkg/b.ts" }]);
    expect(tree.holds("pkg/b.ts")).toBe(true);
    expect(tree.holds("pkg/inner/a.ts")).toBe(true);
  });

  it("removes a deleted chain once, with everything below it", () => {
    const tree = liveTree([ROOT_WITH_CHAIN, INNER]);

    // The stale listing of `pkg/inner` is still cached when the root refreshes.
    const deleted = tree.refresh([{ folder: "", entries: [file("README.md")] }, INNER]);

    expect(deleted.operations).toEqual([
      { type: "remove", path: "pkg/", recursive: true },
      { type: "add", path: "README.md" },
    ]);
    expect(deleted.removedFolders).toEqual(["pkg"]);
    expect(tree.holds("pkg/")).toBe(false);
    expect(tree.holds("pkg/inner/a.ts")).toBe(false);
    expect(deleted.present).toEqual(new Set(["README.md"]));
    expect(deleted.loadedFolders).toEqual(new Set([""]));
  });

  it("adds a folder named by both a chain hint and its parent's listing once", () => {
    const tree = liveTree([
      ROOT_WITH_CHAIN,
      { folder: "pkg", entries: [folder("pkg/inner")] },
      INNER,
    ]);

    const again = tree.refresh([
      ROOT_WITH_CHAIN,
      { folder: "pkg", entries: [folder("pkg/inner")] },
      INNER,
    ]);

    const innerAdds = tree.first.operations.filter(
      (change) => change.type === "add" && change.path === "pkg/inner/",
    );
    expect(innerAdds).toHaveLength(1);
    expect(again.operations).toEqual([]);
  });

  it("removes a chain's deepest folder when its parent's own listing drops it", () => {
    const tree = liveTree([ROOT_WITH_CHAIN, INNER]);

    const emptied = tree.refresh([
      { folder: "", entries: [folder("pkg")] },
      { folder: "pkg", entries: [file("pkg/b.ts")] },
      INNER,
    ]);

    expect(emptied.operations).toEqual([
      { type: "remove", path: "pkg/inner/", recursive: true },
      { type: "add", path: "pkg/b.ts" },
    ]);
    expect(tree.holds("pkg/inner/")).toBe(false);
    expect(tree.holds("pkg/b.ts")).toBe(true);
  });

  it("adds and removes plain entries of a refreshed folder", () => {
    const tree = liveTree([{ folder: "", entries: [folder("lib"), file("app.ts")] }]);

    const refreshed = tree.refresh([{ folder: "", entries: [file("app.ts"), file("main.ts")] }]);

    expect(refreshed.operations).toEqual([
      { type: "remove", path: "lib/", recursive: true },
      { type: "add", path: "main.ts" },
    ]);
    expect(tree.holds("lib/")).toBe(false);
    expect(tree.holds("main.ts")).toBe(true);
  });

  it("lets a folder's own listing outrank a stale chain hint, sync after sync", () => {
    // The root was cached while `inner` was a folder; `pkg`'s fresher listing has it as a file.
    const listings: FolderListing[] = [
      ROOT_WITH_CHAIN,
      { folder: "pkg", entries: [file("pkg/inner")] },
    ];
    const tree = liveTree(listings);
    expect(tree.first.present).toEqual(new Set(["pkg/", "pkg/inner"]));

    const again = tree.refresh(listings);

    expect(again.operations).toEqual([]);
    expect(tree.holds("pkg/inner")).toBe(true);
  });

  it("applies a folder that became a file in its parent's listing", () => {
    const tree = liveTree([
      ROOT_WITH_CHAIN,
      { folder: "pkg", entries: [folder("pkg/inner")] },
      INNER,
    ]);

    const changed = tree.refresh([
      ROOT_WITH_CHAIN,
      { folder: "pkg", entries: [file("pkg/inner")] },
      INNER,
    ]);

    expect(changed.operations).toEqual([
      { type: "remove", path: "pkg/inner/", recursive: true },
      { type: "add", path: "pkg/inner" },
    ]);
    expect(tree.holds("pkg/inner")).toBe(true);
  });

  it("never creates a hinted folder through a file the tree holds", () => {
    const tree = liveTree([
      { folder: "", entries: [folder("pkg")] },
      { folder: "pkg", entries: [file("pkg/inner")] },
    ]);

    // Only the root refreshes, with a hint that disagrees with the file the tree knows.
    const hinted = tree.refresh([ROOT_WITH_CHAIN]);

    expect(hinted.operations).toEqual([]);
    expect(tree.holds("pkg/inner")).toBe(true);
  });
});

describe("visibleTreeRow", () => {
  const present = new Set([
    "App/",
    "App/Sub/",
    "App/Sub/Deep/",
    "Two/",
    "Two/a.ts",
    "Two/b/",
    "x.ts",
  ]);

  it("finds a file and a folder that show as their own rows", () => {
    expect(visibleTreeRow(present, "x.ts")).toBe("x.ts");
    expect(visibleTreeRow(present, "Two")).toBe("Two/");
  });

  it("finds a single-folder chain's row by its deepest folder, as the real tree shows it", () => {
    expect(visibleTreeRow(present, "App")).toBe("App/Sub/Deep/");
    expect(visibleTreeRow(present, "App/Sub")).toBe("App/Sub/Deep/");
    const real = new FileTreeModel({ paths: [...present], flattenEmptyDirectories: true });
    expect(real.getVisibleRows(0, 10).map((row) => row.path)).toContain("App/Sub/Deep/");
  });

  it("says nothing for an entry the tree doesn't hold", () => {
    expect(visibleTreeRow(present, "missing")).toBeUndefined();
    expect(visibleTreeRow(new Set(), "")).toBeUndefined();
  });
});
