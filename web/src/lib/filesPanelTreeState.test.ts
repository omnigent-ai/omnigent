import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_EXPANDED_PATHS,
  MAX_STORE_CHARS,
  readFilesTreeBrowseLocation,
  readFilesTreeExpandedPaths,
  writeFilesTreeBrowseLocation,
  writeFilesTreeExpandedPaths,
} from "./filesPanelTreeState";

const STORAGE_KEY = "omnigent:files-panel-tree-state";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("filesPanelTreeState", () => {
  it("remembers each session's browsed folder separately", () => {
    expect(readFilesTreeBrowseLocation("conv_a")).toBeNull();

    writeFilesTreeBrowseLocation("conv_a", "/home/user/proj/src");
    writeFilesTreeBrowseLocation("conv_b", "/tmp");
    expect(readFilesTreeBrowseLocation("conv_a")).toBe("/home/user/proj/src");
    expect(readFilesTreeBrowseLocation("conv_b")).toBe("/tmp");

    writeFilesTreeBrowseLocation("conv_a", null);
    expect(readFilesTreeBrowseLocation("conv_a")).toBeNull();
  });

  it("keeps expansion per location and tells an untouched location from a collapsed one", () => {
    expect(readFilesTreeExpandedPaths("conv_a", "")).toBeUndefined();

    writeFilesTreeExpandedPaths("conv_a", "", new Set(["src", "src/lib"]));
    writeFilesTreeExpandedPaths("conv_a", "docs", ["guides"]);
    expect(readFilesTreeExpandedPaths("conv_a", "")).toEqual(["src", "src/lib"]);
    expect(readFilesTreeExpandedPaths("conv_a", "docs")).toEqual(["guides"]);
    expect(readFilesTreeExpandedPaths("conv_b", "")).toBeUndefined();

    // Collapsing everything is a choice to restore, not a missing entry.
    writeFilesTreeExpandedPaths("conv_a", "", []);
    expect(readFilesTreeExpandedPaths("conv_a", "")).toEqual([]);
  });

  it("keeps the browsed folder and the expansion of one session together", () => {
    writeFilesTreeBrowseLocation("conv_a", "/home/user/proj/src");
    writeFilesTreeExpandedPaths("conv_a", "src", ["components"]);
    expect(readFilesTreeBrowseLocation("conv_a")).toBe("/home/user/proj/src");
    expect(readFilesTreeExpandedPaths("conv_a", "src")).toEqual(["components"]);
  });

  it("keeps the shallowest folders when a location has too many expanded", () => {
    const deep = Array.from({ length: MAX_EXPANDED_PATHS }, (_, index) => `a/b/${index}`);
    writeFilesTreeExpandedPaths("conv_a", "", [...deep, "a", "a/b"]);
    const kept = readFilesTreeExpandedPaths("conv_a", "");
    expect(kept).toHaveLength(MAX_EXPANDED_PATHS);
    expect(kept?.slice(0, 2)).toEqual(["a", "a/b"]);
    expect(kept).not.toContain(`a/b/${MAX_EXPANDED_PATHS - 1}`);
  });

  it("forgets the least recently changed locations and sessions first", () => {
    for (let index = 0; index < 10; index++) {
      writeFilesTreeExpandedPaths("conv_a", `dir${index}`, ["x"]);
    }
    // Changing the oldest location again makes dir1 the oldest instead.
    writeFilesTreeExpandedPaths("conv_a", "dir0", ["y"]);
    writeFilesTreeExpandedPaths("conv_a", "dir10", ["x"]);
    expect(readFilesTreeExpandedPaths("conv_a", "dir0")).toEqual(["y"]);
    expect(readFilesTreeExpandedPaths("conv_a", "dir1")).toBeUndefined();
    expect(readFilesTreeExpandedPaths("conv_a", "dir10")).toEqual(["x"]);

    for (let index = 0; index < 30; index++) {
      writeFilesTreeBrowseLocation(`conv_${index}`, "/tmp");
    }
    writeFilesTreeBrowseLocation("conv_0", "/srv");
    writeFilesTreeBrowseLocation("conv_30", "/tmp");
    expect(readFilesTreeBrowseLocation("conv_0")).toBe("/srv");
    expect(readFilesTreeBrowseLocation("conv_1")).toBeNull();
    expect(readFilesTreeBrowseLocation("conv_30")).toBe("/tmp");
  });

  it("keeps the whole store within its size budget, dropping the oldest sessions", () => {
    const longPaths = Array.from(
      { length: MAX_EXPANDED_PATHS },
      (_, index) => `${"deeply-nested-folder-name/".repeat(4)}${index}`,
    );
    for (let index = 0; index < 8; index++) {
      writeFilesTreeExpandedPaths(`conv_${index}`, "", longPaths);
    }

    expect(localStorage.getItem(STORAGE_KEY)!.length).toBeLessThanOrEqual(MAX_STORE_CHARS);
    expect(readFilesTreeExpandedPaths("conv_0", "")).toBeUndefined();
    expect(readFilesTreeExpandedPaths("conv_7", "")).toHaveLength(MAX_EXPANDED_PATHS);
  });

  it("trims a session too big on its own before forgetting other sessions", () => {
    writeFilesTreeBrowseLocation("conv_other", "/srv");
    const bulkyPaths = Array.from(
      { length: MAX_EXPANDED_PATHS },
      (_, index) => `${"y".repeat(100)}/${index}`,
    );
    for (let location = 0; location < 10; location++) {
      writeFilesTreeExpandedPaths("conv_big", `dir${location}`, bulkyPaths);
    }

    expect(localStorage.getItem(STORAGE_KEY)!.length).toBeLessThanOrEqual(MAX_STORE_CHARS);
    expect(readFilesTreeBrowseLocation("conv_other")).toBe("/srv");
    expect(readFilesTreeExpandedPaths("conv_big", "dir0")).toBeUndefined();
    expect(readFilesTreeExpandedPaths("conv_big", "dir9")).toHaveLength(MAX_EXPANDED_PATHS);
  });

  it("drops a single location too large for the budget but keeps the browsed folder", () => {
    const hugePaths = Array.from(
      { length: MAX_EXPANDED_PATHS },
      (_, index) => `${"x".repeat(1_000)}/${index}`,
    );
    writeFilesTreeBrowseLocation("conv_a", "/srv");
    writeFilesTreeExpandedPaths("conv_a", "", hugePaths);

    expect(localStorage.getItem(STORAGE_KEY)!.length).toBeLessThanOrEqual(MAX_STORE_CHARS);
    expect(readFilesTreeExpandedPaths("conv_a", "")).toBeUndefined();
    expect(readFilesTreeBrowseLocation("conv_a")).toBe("/srv");
  });

  it("keeps navigation on this page when storage refuses the write", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    writeFilesTreeBrowseLocation("conv_a", "/srv");
    writeFilesTreeExpandedPaths("conv_a", "", ["src"]);
    expect(readFilesTreeBrowseLocation("conv_a")).toBe("/srv");
    expect(readFilesTreeExpandedPaths("conv_a", "")).toEqual(["src"]);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();

    // Another tab saves its session meanwhile; this page still sees it.
    setItem.mockRestore();
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([{ id: "conv_c", state: { browseLocation: "/opt", expanded: [] } }]),
    );
    expect(readFilesTreeBrowseLocation("conv_c")).toBe("/opt");
    expect(readFilesTreeBrowseLocation("conv_a")).toBe("/srv");

    // The next accepted write saves this page's sessions without losing the other tab's.
    writeFilesTreeExpandedPaths("conv_b", "", ["docs"]);
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!) as { id: string }[];
    expect(saved.map((entry) => entry.id)).toEqual(["conv_c", "conv_a", "conv_b"]);
    expect(readFilesTreeBrowseLocation("conv_a")).toBe("/srv");
  });

  it("treats malformed storage as empty and recovers on the next write", () => {
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readFilesTreeBrowseLocation("conv_a")).toBeNull();
    expect(readFilesTreeExpandedPaths("conv_a", "")).toBeUndefined();

    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([{ id: "conv_a", state: { expanded: [{ location: "", paths: "src" }] } }]),
    );
    expect(readFilesTreeExpandedPaths("conv_a", "")).toBeUndefined();

    writeFilesTreeExpandedPaths("conv_a", "", ["src"]);
    expect(readFilesTreeExpandedPaths("conv_a", "")).toEqual(["src"]);
  });
});
