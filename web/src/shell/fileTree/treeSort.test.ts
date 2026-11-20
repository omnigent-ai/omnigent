import { describe, expect, it } from "vitest";
import { FileTreeModel } from "@/components/file-tree/vendored";
import type { ChangedSort } from "../FlatFileList";
import { type TreeEntryMeta, treeComparator, treeOrderSignature } from "./treeSort";

const PATHS = ["b.ts", "a.ts", "src/", "src/z.ts", "src/y.ts", "docs/", "docs/x.md"];
const META = new Map<string, TreeEntryMeta>([
  ["a.ts", { bytes: 10, modified_at: 100 }],
  ["b.ts", { bytes: 30, modified_at: 300 }],
  ["src", { bytes: null, modified_at: 50 }],
  ["src/y.ts", { bytes: 5, modified_at: 10 }],
  ["src/z.ts", { bytes: 50, modified_at: 20 }],
  ["docs", { bytes: null, modified_at: 400 }],
  ["docs/x.md", { bytes: 1, modified_at: 1 }],
]);

/** The visible rows' paths, top to bottom. */
function rows(tree: FileTreeModel): string[] {
  return tree.getVisibleRows(0, tree.getVisibleCount()).map((row) => row.path);
}

/** A comparator whose sort and metadata can change, as the panel's does. */
function liveComparator(state: { sort: ChangedSort; meta: Map<string, TreeEntryMeta> }) {
  return (
    left: Parameters<ReturnType<typeof treeComparator>>[0],
    right: Parameters<ReturnType<typeof treeComparator>>[1],
  ) => treeComparator(state.sort, state.meta)(left, right);
}

function makeTree(sort: ChangedSort, meta = META) {
  const state = { sort, meta: new Map(meta) };
  const tree = new FileTreeModel({
    paths: PATHS,
    // As the panel builds it: closed, with the open folders named, so a reset keeps them.
    initialExpansion: "closed",
    initialExpandedPaths: ["src/", "docs/"],
    sort: liveComparator(state),
  });
  return { tree, state };
}

describe("treeComparator", () => {
  it("puts folders first, then sorts by name", () => {
    const { tree } = makeTree("alpha");
    expect(rows(tree)).toEqual([
      "docs/",
      "docs/x.md",
      "src/",
      "src/y.ts",
      "src/z.ts",
      "a.ts",
      "b.ts",
    ]);
  });

  it("sorts by last edited, folders by their own mtime", () => {
    const { tree } = makeTree("recent");
    expect(rows(tree)).toEqual([
      "docs/",
      "docs/x.md",
      "src/",
      "src/z.ts",
      "src/y.ts",
      "b.ts",
      "a.ts",
    ]);
  });

  it("sorts files by size and folders, which have none, by name", () => {
    const { tree } = makeTree("size");
    expect(rows(tree)).toEqual([
      "docs/",
      "docs/x.md",
      "src/",
      "src/z.ts",
      "src/y.ts",
      "b.ts",
      "a.ts",
    ]);
  });

  it("re-sorts in place through resetPaths, keeping what was expanded", () => {
    const { tree, state } = makeTree("alpha");
    const docs = tree.getItem("docs/");
    if (docs && "collapse" in docs) docs.collapse();
    const expanded = rows(tree).filter((path) => {
      const item = tree.getItem(path);
      return item !== null && "isExpanded" in item && item.isExpanded();
    });
    state.sort = "recent";
    tree.resetPaths(PATHS, { initialExpandedPaths: expanded });
    expect(rows(tree)).toEqual(["docs/", "src/", "src/z.ts", "src/y.ts", "b.ts", "a.ts"]);
  });

  it("reorders the same paths when only their metadata changes", () => {
    const { tree, state } = makeTree("recent");
    state.meta.set("a.ts", { bytes: 10, modified_at: 999 });
    tree.resetPaths(PATHS, { initialExpandedPaths: ["src/", "docs/"] });
    expect(rows(tree).slice(-2)).toEqual(["a.ts", "b.ts"]);
  });
});

describe("treeOrderSignature", () => {
  it("changes only when the current sort's field changes", () => {
    const sizes = new Map(META).set("a.ts", { bytes: 999, modified_at: 100 });
    const times = new Map(META).set("a.ts", { bytes: 10, modified_at: 999 });
    expect(treeOrderSignature("alpha", sizes)).toBe(treeOrderSignature("alpha", META));
    expect(treeOrderSignature("recent", sizes)).toBe(treeOrderSignature("recent", META));
    expect(treeOrderSignature("recent", times)).not.toBe(treeOrderSignature("recent", META));
    expect(treeOrderSignature("size", sizes)).not.toBe(treeOrderSignature("size", META));
  });

  it("changes with the sort itself", () => {
    expect(treeOrderSignature("alpha", META)).not.toBe(treeOrderSignature("type", META));
  });
});
