import { afterEach, describe, expect, it } from "vitest";
import {
  MAX_EXPANDED_PATHS,
  readFilesTreeBrowseLocation,
  readFilesTreeExpandedPaths,
  writeFilesTreeBrowseLocation,
  writeFilesTreeExpandedPaths,
} from "./filesPanelTreeState";

const STORAGE_KEY = "omnigent:files-panel-tree-state";

afterEach(() => {
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

  it("forgets the least recently used locations and sessions first", () => {
    for (let index = 0; index < 11; index++) {
      writeFilesTreeExpandedPaths("conv_a", `dir${index}`, ["x"]);
    }
    expect(readFilesTreeExpandedPaths("conv_a", "dir0")).toBeUndefined();
    expect(readFilesTreeExpandedPaths("conv_a", "dir10")).toEqual(["x"]);

    for (let index = 0; index < 31; index++) {
      writeFilesTreeBrowseLocation(`conv_${index}`, "/tmp");
    }
    expect(readFilesTreeBrowseLocation("conv_0")).toBeNull();
    expect(readFilesTreeBrowseLocation("conv_30")).toBe("/tmp");
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
