import { describe, expect, it, vi } from "vitest";
import { FileTreeModel } from "./model";
import { orderTreePaths } from "./order";
import type { FileTreeSortComparator } from "./types";

const visiblePaths = (model: FileTreeModel) =>
  model.getVisibleRows(0, model.getVisibleCount() - 1).map((row) => row.path);

function directory(model: FileTreeModel, path: string) {
  const item = model.getItem(path);
  if (!item || !("expand" in item)) throw new Error(`${path} is not a directory`);
  return item;
}

describe("batch", () => {
  it("adds in place, without rebuilding the tree", () => {
    const model = new FileTreeModel({ paths: ["lib/"], initialExpansion: "open" });
    const listener = vi.fn();
    model.subscribe(listener);
    model.batch([
      { type: "add", path: "lib/a.ts" },
      { type: "add", path: "b.ts" },
    ]);
    expect(visiblePaths(model)).toEqual(["lib/", "lib/a.ts", "b.ts"]);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("removes a file and a folder with its contents, keeping a folder it empties", () => {
    const model = new FileTreeModel({
      paths: ["lib/a.ts", "lib/b.ts", "old/x.ts", "old/deep/y.ts", "src/only.ts", "z.ts"],
      initialExpansion: "open",
    });
    model.batch([
      { type: "remove", path: "lib/a.ts" },
      { type: "remove", path: "old/", recursive: true },
      { type: "remove", path: "src/only.ts" },
      { type: "add", path: "new.ts" },
    ]);
    expect(visiblePaths(model)).toEqual(["lib/", "lib/b.ts", "src/", "new.ts", "z.ts"]);
    expect(model.getItem("old/x.ts")).toBeNull();
  });

  it("keeps open folders, selection and focus on rows that remain", () => {
    const model = new FileTreeModel({
      paths: ["a/one.ts", "a/two.ts", "b/three.ts", "c.ts"],
      initialSelectedPaths: ["a/two.ts", "c.ts"],
    });
    directory(model, "a/").expand();
    const onSelection = vi.fn();
    model.subscribe(onSelection);
    model.batch([{ type: "remove", path: "c.ts" }]);
    expect(visiblePaths(model)).toEqual(["a/", "a/one.ts", "a/two.ts", "b/"]);
    expect(directory(model, "b/").isExpanded()).toBe(false);
    expect(model.getSelectedPaths()).toEqual(["a/two.ts"]);
    // The focused row had no remaining folder above it, so focus returns to the first row.
    expect(model.getFocusedPath()).toBe("a/");
  });

  it("moves focus to the nearest remaining row when the focused row goes", () => {
    const model = new FileTreeModel({ paths: ["a/one.ts", "b.ts"], initialExpansion: "open" });
    model.focusPath("a/one.ts");
    model.batch([{ type: "remove", path: "a/one.ts" }]);
    expect(visiblePaths(model)).toEqual(["a/", "b.ts"]);
    expect(model.getFocusedPath()).toBe("a/");
  });

  it("tells the selection listener when a removal drops a selected row", () => {
    const onSelectionChange = vi.fn();
    const model = new FileTreeModel({
      paths: ["a.ts", "b.ts"],
      initialSelectedPaths: ["a.ts"],
      onSelectionChange,
    });
    model.batch([{ type: "remove", path: "a.ts" }]);
    expect(onSelectionChange).toHaveBeenCalledWith([]);
  });

  it("keeps working after a rebuild: later adds and folder toggles still notify", () => {
    const model = new FileTreeModel({ paths: ["a/one.ts", "b.ts"] });
    model.batch([{ type: "remove", path: "b.ts" }]);
    const listener = vi.fn();
    model.subscribe(listener);
    directory(model, "a/").expand();
    model.addPaths(["a/two.ts"]);
    expect(visiblePaths(model)).toEqual(["a/", "a/one.ts", "a/two.ts"]);
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe("a selected file's folder replaced by a file", () => {
  // The other a.ts names the selected file's basename, so the lookup reaches the new file src.
  it.each([
    [
      "a batch",
      (model: FileTreeModel) =>
        model.batch([
          { type: "remove", path: "src/", recursive: true },
          { type: "add", path: "src" },
        ]),
    ],
    ["a reset", (model: FileTreeModel) => model.resetPaths(["src", "a.ts"])],
  ])("clears the selection through %s, and the tree keeps working", (_, replace) => {
    const model = new FileTreeModel({
      paths: ["src/a.ts", "a.ts"],
      initialExpansion: "open",
      initialSelectedPaths: ["src/a.ts"],
    });

    replace(model);

    expect(model.getSelectedPaths()).toEqual([]);
    expect(visiblePaths(model)).toEqual(["a.ts", "src"]);
    expect(model.getItem("src/a.ts")).toBeNull();
    model.addPaths(["b.ts"]);
    expect(visiblePaths(model)).toEqual(["a.ts", "b.ts", "src"]);
  });
});

describe("resetPaths", () => {
  it("opens exactly the folders it is given", () => {
    const model = new FileTreeModel({ paths: ["a/x.ts", "b/y.ts"], initialExpansion: "open" });
    model.resetPaths(["a/x.ts", "b/y.ts", "c/z.ts"], { initialExpandedPaths: ["c/"] });
    expect(visiblePaths(model)).toEqual(["a/", "b/", "c/", "c/z.ts"]);
  });

  it("keeps open folders without a list, and re-sorts with the current comparator", () => {
    // Comparators order the input paths, so listing folders explicitly lets them compare as siblings.
    let descending = false;
    const sort: FileTreeSortComparator = (left, right) =>
      (descending ? -1 : 1) * left.basename.localeCompare(right.basename);
    const model = new FileTreeModel({ paths: ["a/", "a/x.ts", "b.ts", "c.ts"], sort });
    directory(model, "a/").expand();
    expect(visiblePaths(model)).toEqual(["a/", "a/x.ts", "b.ts", "c.ts"]);
    descending = true;
    model.resetPaths(["a/", "a/x.ts", "b.ts", "c.ts"]);
    // "a/x.ts" sorts by its own name, x, so the folder holding it comes first.
    expect(visiblePaths(model)).toEqual(["a/", "a/x.ts", "c.ts", "b.ts"]);
  });
});

describe("sort", () => {
  it("orders siblings, and places added paths by the same comparator", () => {
    const bySize = new Map([
      ["big.ts", 3],
      ["mid.ts", 2],
      ["small.ts", 1],
    ]);
    const sort: FileTreeSortComparator = (left, right) =>
      (bySize.get(right.path) ?? 0) - (bySize.get(left.path) ?? 0);
    const model = new FileTreeModel({ paths: ["small.ts", "big.ts"], sort });
    model.addPaths(["mid.ts"]);
    expect(visiblePaths(model)).toEqual(["big.ts", "mid.ts", "small.ts"]);
  });

  it("opens the folders it starts with under a custom order, as under the default", () => {
    const reverse: FileTreeSortComparator = (left, right) =>
      right.basename.localeCompare(left.basename);
    const model = new FileTreeModel({
      paths: ["a/", "a/x.ts", "b/", "b/y.ts", "c.ts"],
      sort: reverse,
      initialExpandedPaths: ["a/"],
    });
    expect(directory(model, "a/").isExpanded()).toBe(true);
    expect(visiblePaths(model)).toContain("a/x.ts");
  });
});

describe("setGitStatus", () => {
  it("replaces the statuses and their folder marks, and repaints", () => {
    const model = new FileTreeModel({
      paths: ["a/x.ts", "b.ts"],
      gitStatus: [{ path: "a/x.ts", status: "modified" }],
    });
    expect(model.gitStatus?.directoriesWithChanges.has("a/")).toBe(true);
    const listener = vi.fn();
    model.subscribe(listener);
    model.setGitStatus([{ path: "b.ts", status: "added" }]);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(model.gitStatus?.statusByPath.get("b.ts")).toBe("added");
    expect(model.gitStatus?.directoriesWithChanges.has("a/")).toBe(false);
    model.setGitStatus([]);
    expect(model.gitStatus).toBeNull();
  });
});

describe("orderTreePaths", () => {
  it("lists paths as the tree shows them: folders first at each level, by name", () => {
    const paths = ["z.ts", "src/b.ts", "src/lib/a.ts", "README.md", "src/a.ts"];
    expect(orderTreePaths(paths, { flattenEmptyDirectories: true })).toEqual([
      "src/lib/a.ts",
      "src/a.ts",
      "src/b.ts",
      "README.md",
      "z.ts",
    ]);
    const model = new FileTreeModel({ paths, initialExpansion: "open" });
    const files = visiblePaths(model).filter((path) => !path.endsWith("/"));
    expect(orderTreePaths(paths)).toEqual(files);
  });
});
