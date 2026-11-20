import { describe, expect, it, vi } from "vitest";
import { resolveFileTreeGitStatusState, effectiveGitStatus } from "./gitStatus";
import { resolveFileIcon } from "./icons";
import { FileTreeModel } from "./model";
import { computeFileTreeRowClickPlan } from "./rowClickPlan";
import { computeScrollTarget, computeWindow } from "./scroll";
import type { FileTreeOptions } from "./types";

const PATHS = [
  "README.md",
  "package.json",
  "src/index.ts",
  "src/components/Button.tsx",
  "src/components/Card.tsx",
  "src/utils/worker.ts",
  "test/index.test.ts",
];

function visiblePaths(model: FileTreeModel): string[] {
  return model.getVisibleRows(0, model.getVisibleCount() - 1).map((row) => row.path);
}

function directory(model: FileTreeModel, path: string) {
  const item = model.getItem(path);
  if (!item || !("expand" in item)) throw new Error(`${path} is not a directory`);
  return item;
}

function create(options: Partial<FileTreeOptions> = {}) {
  return new FileTreeModel({ paths: PATHS, ...options });
}

describe("FileTreeModel structure", () => {
  it("lists directories before files in case-insensitive natural order, collapsed by default", () => {
    const model = new FileTreeModel({
      paths: ["b.txt", "a10.txt", "a2.txt", "dir/x.ts", "A1.txt"],
    });

    expect(visiblePaths(model)).toEqual(["dir/", "A1.txt", "a2.txt", "a10.txt", "b.txt"]);
  });

  it("opens every directory with initialExpansion open", () => {
    const model = create({ initialExpansion: "open" });

    expect(visiblePaths(model)).toEqual([
      "src/",
      "src/components/",
      "src/components/Button.tsx",
      "src/components/Card.tsx",
      "src/utils/",
      "src/utils/worker.ts",
      "src/index.ts",
      "test/",
      "test/index.test.ts",
      "package.json",
      "README.md",
    ]);
  });

  it("reports depth and sibling position for assistive technology", () => {
    const model = create({ initialExpansion: "open" });
    const card = model.getVisibleRows(3, 3)[0];

    expect(card).toMatchObject({
      path: "src/components/Card.tsx",
      depth: 2,
      posInSet: 1,
      setSize: 2,
      ancestorPaths: ["src/", "src/components/"],
      kind: "file",
    });
  });

  it("flattens single-child directory chains into one row", () => {
    const model = new FileTreeModel({
      paths: ["rust/helper/src/main.rs", "README.md"],
      flattenEmptyDirectories: true,
      initialExpansion: "open",
    });
    const [chain] = model.getVisibleRows(0, 0);

    expect(chain.isFlattened).toBe(true);
    expect(chain.flattenedSegments?.map((segment) => segment.name)).toEqual([
      "rust",
      "helper",
      "src",
    ]);
    expect(chain.flattenedSegments?.at(-1)).toMatchObject({
      isTerminal: true,
      path: "rust/helper/src/",
    });
    expect(visiblePaths(model)).toContain("rust/helper/src/main.rs");
  });

  it("expands and collapses directories through item handles", () => {
    const model = create();
    directory(model, "src").expand();
    expect(visiblePaths(model)).toEqual([
      "src/",
      "src/components/",
      "src/utils/",
      "src/index.ts",
      "test/",
      "package.json",
      "README.md",
    ]);

    directory(model, "src/").collapse();
    expect(visiblePaths(model)).toEqual(["src/", "test/", "package.json", "README.md"]);
  });

  it("expands a nested directory's ancestors so it becomes visible", () => {
    const model = create();
    directory(model, "src/components/").expand();

    expect(visiblePaths(model)).toContain("src/components/Button.tsx");
    expect(directory(model, "src/").isExpanded()).toBe(true);
  });
});

describe("FileTreeModel growth", () => {
  it("adds paths under collapsed directories without revealing them", () => {
    const model = new FileTreeModel({ paths: ["lib/", "README.md"] });
    model.addPaths(["lib/a.ts", "lib/b.ts"]);

    expect(visiblePaths(model)).toEqual(["lib/", "README.md"]);
    directory(model, "lib/").expand();
    expect(visiblePaths(model)).toEqual(["lib/", "lib/a.ts", "lib/b.ts", "README.md"]);
  });

  it("keeps selection and focus while children load", () => {
    const model = new FileTreeModel({
      paths: ["lib/", "README.md"],
      initialSelectedPaths: ["README.md"],
    });
    directory(model, "lib/").expand();
    model.addPaths(["lib/a.ts"]);

    expect(model.getSelectedPaths()).toEqual(["README.md"]);
    expect(model.getFocusedPath()).toBe("README.md");
    expect(visiblePaths(model)).toEqual(["lib/", "lib/a.ts", "README.md"]);
  });

  it("notifies subscribers once per batch", () => {
    const model = new FileTreeModel({ paths: ["lib/"], initialExpansion: "open" });
    const listener = vi.fn();
    model.subscribe(listener);
    const version = model.getVersion();

    model.addPaths(["lib/a.ts", "lib/b.ts", "lib/c.ts"]);

    expect(listener).toHaveBeenCalledTimes(1);
    expect(model.getVersion()).toBeGreaterThan(version);
  });

  it("ignores an empty batch", () => {
    const model = create();
    const listener = vi.fn();
    model.subscribe(listener);

    model.addPaths([]);

    expect(listener).not.toHaveBeenCalled();
  });
});

describe("FileTreeModel focus and selection", () => {
  it("focuses the last initially selected path", () => {
    const model = create({
      initialExpansion: "open",
      initialSelectedPaths: ["README.md", "src/index.ts"],
    });

    expect(model.getSelectedPaths()).toEqual(["README.md", "src/index.ts"]);
    expect(model.getFocusedPath()).toBe("src/index.ts");
  });

  it("moves focus through visible rows and to the parent", () => {
    const model = create({ initialExpansion: "open" });
    model.focusFirstItem();
    model.focusNextItem();
    expect(model.getFocusedPath()).toBe("src/components/");

    model.focusNextItem();
    model.focusParentItem();
    expect(model.getFocusedPath()).toBe("src/components/");

    model.focusLastItem();
    expect(model.getFocusedPath()).toBe("README.md");
    model.focusNextItem();
    expect(model.getFocusedPath()).toBe("README.md");
    model.focusPreviousItem();
    expect(model.getFocusedPath()).toBe("package.json");
  });

  it("reports selection changes but not focus-only changes", () => {
    const onSelectionChange = vi.fn();
    const model = create({ initialExpansion: "open", onSelectionChange });

    model.focusNextItem();
    expect(onSelectionChange).not.toHaveBeenCalled();

    model.getItem("README.md")?.select();
    expect(onSelectionChange).toHaveBeenLastCalledWith(["README.md"]);
    model.getItem("README.md")?.select();
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
  });

  it("selects a range from the anchor and toggles single rows", () => {
    const model = create({ initialExpansion: "open" });
    model.selectOnlyMountedPathFromInput("src/components/Card.tsx");
    model.selectPathRange("src/index.ts", false);
    expect(model.getSelectedPaths()).toEqual([
      "src/components/Card.tsx",
      "src/utils/",
      "src/utils/worker.ts",
      "src/index.ts",
    ]);

    model.togglePathSelectionFromInput("src/utils/");
    expect(model.getSelectedPaths()).not.toContain("src/utils/");
  });

  it("extends and shrinks a keyboard selection from the focused row", () => {
    const model = create({ initialExpansion: "open", initialSelectedPaths: ["test/"] });
    model.extendSelectionFromFocused(1);
    model.extendSelectionFromFocused(1);
    expect(model.getSelectedPaths()).toEqual(["test/", "test/index.test.ts", "package.json"]);

    model.extendSelectionFromFocused(-1);
    expect(model.getSelectedPaths()).toEqual(["test/", "test/index.test.ts"]);
  });

  it("selects every visible row", () => {
    const model = create();
    model.selectAllVisiblePaths();

    expect(model.getSelectedPaths()).toEqual(["src/", "test/", "package.json", "README.md"]);
  });
});

describe("FileTreeModel search", () => {
  it("hides non-matching rows and opens the ancestors of matches", () => {
    const model = create();
    model.setSearch("but");

    expect(visiblePaths(model)).toEqual(["src/", "src/components/", "src/components/Button.tsx"]);
    expect(model.getFocusedPath()).toBe("src/components/Button.tsx");
  });

  it("matches case-insensitively and trims the query", () => {
    const model = create();
    model.setSearch("  CARD ");

    expect(visiblePaths(model)).toContain("src/components/Card.tsx");
  });

  it("restores the previous expansion, keeping the selection revealed", () => {
    const model = create();
    model.setSearch("worker");
    model.selectOnlyMountedPathFromInput("src/utils/worker.ts");
    model.setSearch(null);

    expect(visiblePaths(model)).toEqual([
      "src/",
      "src/components/",
      "src/utils/",
      "src/utils/worker.ts",
      "src/index.ts",
      "test/",
      "package.json",
      "README.md",
    ]);
  });

  it("shows every row collapsed when nothing matches", () => {
    const model = create({ initialExpansion: "open" });
    model.setSearch("zzz");

    expect(visiblePaths(model)).toEqual(["src/", "test/", "package.json", "README.md"]);
  });

  it("keeps a directory the user collapsed during a search collapsed", () => {
    const model = create();
    model.setSearch("ts");
    model.toggleMountedDirectoryFromInput("src/components/");

    expect(visiblePaths(model)).not.toContain("src/components/Button.tsx");
    expect(visiblePaths(model)).toContain("src/components/");
    model.setSearch("tsx");
    expect(visiblePaths(model)).not.toContain("src/components/Button.tsx");
  });

  it("applies an initial search query", () => {
    const model = create({ initialSearchQuery: "index" });

    expect(model.isSearchOpen()).toBe(true);
    expect(visiblePaths(model)).toEqual(["src/", "src/index.ts", "test/", "test/index.test.ts"]);
  });
});

describe("FileTreeModel scroll requests", () => {
  it("records a one-shot request and focuses the target by default", () => {
    const model = create({ initialExpansion: "open" });
    model.scrollToPath("test/index.test.ts", { offset: "center" });

    const request = model.getScrollRequest();
    expect(request).toMatchObject({ offset: "center", visibleIndex: 8 });
    expect(model.getFocusedPath()).toBe("test/index.test.ts");
    model.clearScrollRequest(request!.id);
    expect(model.getScrollRequest()).toBeNull();
  });

  it("can reveal a row without moving focus", () => {
    const model = create({ initialExpansion: "open", initialSelectedPaths: ["README.md"] });
    model.scrollToPath("src/index.ts", { focus: false });

    expect(model.getScrollRequest()).toMatchObject({ offset: "nearest", visibleIndex: 6 });
    expect(model.getFocusedPath()).toBe("README.md");
  });

  it("ignores paths that are hidden or unknown", () => {
    const model = create();
    model.scrollToPath("src/index.ts");
    model.scrollToPath("missing.txt");

    expect(model.getScrollRequest()).toBeNull();
  });
});

describe("git status", () => {
  it("marks ancestors of changed paths and inherits ignored directories", () => {
    const state = resolveFileTreeGitStatusState([
      { path: "src/components/Button.tsx", status: "modified" },
      { path: "build/", status: "ignored" },
    ])!;

    expect([...state.directoriesWithChanges]).toEqual(["src/", "src/components/"]);
    expect(effectiveGitStatus(state, "src/components/Button.tsx", [])).toBe("modified");
    expect(effectiveGitStatus(state, "build/out.js", ["build/"])).toBe("ignored");
    expect(effectiveGitStatus(state, "src/index.ts", ["src/"])).toBeNull();
  });

  it("normalizes separators and treats an empty list as no status", () => {
    expect(resolveFileTreeGitStatusState([])).toBeNull();
    const state = resolveFileTreeGitStatusState([{ path: "/a//b.ts", status: "added" }])!;
    expect(state.statusByPath.get("a/b.ts")).toBe("added");
  });
});

describe("file icons", () => {
  it.each([
    ["package.json", "json"],
    ["Dockerfile", "docker"],
    ["src/App.tsx", "react"],
    ["src/main.rs", "rust"],
    ["types.d.ts", "typescript"],
    ["Makefile", "default"],
    ["notes", "default"],
  ])("resolves %s to %s", (path, token) => {
    expect(resolveFileIcon(path)).toEqual({ name: `file-tree-builtin-${token}`, token });
  });
});

describe("row clicks", () => {
  const click = { shiftKey: false, ctrlKey: false, metaKey: false };

  it("selects the row and toggles directories on a plain click", () => {
    expect(computeFileTreeRowClickPlan(click, true)).toEqual({
      selection: { kind: "single" },
      toggleDirectory: true,
    });
    expect(computeFileTreeRowClickPlan(click, false).toggleDirectory).toBe(false);
  });

  it("uses modifiers for range and toggle selection without opening directories", () => {
    expect(computeFileTreeRowClickPlan({ ...click, shiftKey: true }, true)).toEqual({
      selection: { kind: "range", additive: false },
      toggleDirectory: false,
    });
    expect(
      computeFileTreeRowClickPlan({ ...click, shiftKey: true, metaKey: true }, false).selection,
    ).toEqual({ kind: "range", additive: true });
    expect(computeFileTreeRowClickPlan({ ...click, ctrlKey: true }, true)).toEqual({
      selection: { kind: "toggle" },
      toggleDirectory: false,
    });
  });
});

describe("scroll geometry", () => {
  const base = { itemHeight: 30, totalHeight: 3000, viewportHeight: 300 };

  it("scrolls the minimum distance for nearest", () => {
    expect(
      computeScrollTarget({ ...base, currentScrollTop: 0, index: 5, offset: "nearest" }),
    ).toBeNull();
    expect(
      computeScrollTarget({ ...base, currentScrollTop: 0, index: 20, offset: "nearest" }),
    ).toBe(330);
    expect(
      computeScrollTarget({ ...base, currentScrollTop: 600, index: 10, offset: "nearest" }),
    ).toBe(300);
  });

  it("aligns to the top or center, clamped to the scrollable range", () => {
    expect(computeScrollTarget({ ...base, currentScrollTop: 0, index: 20, offset: "top" })).toBe(
      600,
    );
    expect(computeScrollTarget({ ...base, currentScrollTop: 0, index: 20, offset: "center" })).toBe(
      465,
    );
    expect(computeScrollTarget({ ...base, currentScrollTop: 0, index: 99, offset: "top" })).toBe(
      2700,
    );
    expect(
      computeScrollTarget({ ...base, currentScrollTop: 0, index: 2, offset: "center" }),
    ).toBeNull();
  });

  it("renders the visible rows plus overscan", () => {
    expect(
      computeWindow({
        itemCount: 100,
        itemHeight: 30,
        overscan: 5,
        scrollTop: 300,
        viewportHeight: 300,
      }),
    ).toEqual({ start: 5, end: 24 });
    expect(
      computeWindow({
        itemCount: 3,
        itemHeight: 30,
        overscan: 5,
        scrollTop: 0,
        viewportHeight: 300,
      }),
    ).toEqual({ start: 0, end: 2 });
    expect(
      computeWindow({
        itemCount: 0,
        itemHeight: 30,
        overscan: 5,
        scrollTop: 0,
        viewportHeight: 300,
      }).end,
    ).toBeLessThan(0);
  });
});
