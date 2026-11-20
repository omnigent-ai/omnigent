import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { rememberBrowseLocation, rememberedBrowseLocation } from "../filesPane/browseLocations";
import {
  ALREADY_ROOT_REASON,
  AT_WORKING_FOLDER_REASON,
  NO_ACTIVE_FOLDER_REASON,
  OUTSIDE_REACH_REASON,
  planActiveFolderAsTreeRoot,
  planFolderAsTreeRoot,
  planWorkingFolderReturn,
  showTreeRoot,
  useFilesTreeShowRequests,
} from "./treeRoot";

const context = { workspaceRoot: "/repo", canBrowseOutside: true };

describe("planFolderAsTreeRoot", () => {
  it("roots the tree at a workspace folder, remembered absolutely", () => {
    expect(
      planFolderAsTreeRoot({ folder: "experimental/iulian.dragos", browsed: null, ...context }),
    ).toEqual({ availability: { available: true }, root: "/repo/experimental/iulian.dragos" });
  });

  it("names the workspace root itself as no browsed folder", () => {
    expect(planFolderAsTreeRoot({ folder: "", browsed: "/repo/src", ...context })).toEqual({
      availability: { available: true },
      root: null,
    });
  });

  it("says when the tree already shows the folder", () => {
    for (const browsed of ["/repo/src", "/repo/src/"]) {
      expect(planFolderAsTreeRoot({ folder: "src", browsed, ...context }).availability).toEqual({
        available: false,
        reason: ALREADY_ROOT_REASON,
      });
    }
    expect(planFolderAsTreeRoot({ folder: "", browsed: null, ...context }).availability).toEqual({
      available: false,
      reason: ALREADY_ROOT_REASON,
    });
  });

  it("keeps a folder outside the workspace to viewers who may browse there", () => {
    expect(planFolderAsTreeRoot({ folder: "/tmp/notes", browsed: null, ...context })).toEqual({
      availability: { available: true },
      root: "/tmp/notes",
    });
    expect(
      planFolderAsTreeRoot({
        folder: "/tmp/notes",
        browsed: null,
        workspaceRoot: "/repo",
        canBrowseOutside: false,
      }).availability,
    ).toEqual({ available: false, reason: OUTSIDE_REACH_REASON });
  });

  it("handles a workspace at the filesystem root", () => {
    const atRoot = { workspaceRoot: "/", canBrowseOutside: false };
    expect(planFolderAsTreeRoot({ folder: "tmp", browsed: null, ...atRoot })).toEqual({
      availability: { available: true },
      root: "/tmp",
    });
    // Everything is inside a workspace at "/", so a collaborator may root anywhere in it.
    expect(planFolderAsTreeRoot({ folder: "/tmp/notes", browsed: null, ...atRoot })).toEqual({
      availability: { available: true },
      root: "/tmp/notes",
    });
    expect(planFolderAsTreeRoot({ folder: "/", browsed: null, ...atRoot }).availability).toEqual({
      available: false,
      reason: ALREADY_ROOT_REASON,
    });
    expect(planWorkingFolderReturn({ browsed: "/tmp", workspaceRoot: "/" }).availability).toEqual({
      available: true,
    });
    expect(
      planWorkingFolderReturn({ browsed: "/", workspaceRoot: "/" }).availability,
    ).toMatchObject({ available: false });
  });

  it("doesn't take a sibling with a shared prefix for a folder inside the workspace", () => {
    expect(
      planFolderAsTreeRoot({
        folder: "/repo-old/src",
        browsed: null,
        workspaceRoot: "/repo",
        canBrowseOutside: false,
      }).availability,
    ).toEqual({ available: false, reason: OUTSIDE_REACH_REASON });
  });

  it("offers nothing until the workspace root is known", () => {
    expect(
      planFolderAsTreeRoot({
        folder: "src",
        browsed: null,
        workspaceRoot: null,
        canBrowseOutside: true,
      }).availability,
    ).toEqual({ available: false });
  });
});

describe("planActiveFolderAsTreeRoot", () => {
  it("roots the tree at the viewed file's folder", () => {
    expect(
      planActiveFolderAsTreeRoot({
        activePath: "experimental/iulian.dragos/OWNERS",
        browsed: null,
        ...context,
      }).root,
    ).toBe("/repo/experimental/iulian.dragos");
    expect(
      planActiveFolderAsTreeRoot({ activePath: "/tmp/a.md", browsed: null, ...context }).root,
    ).toBe("/tmp");
  });

  it("waits for an open file, and says when its folder is already the root", () => {
    expect(
      planActiveFolderAsTreeRoot({ activePath: null, browsed: null, ...context }).availability,
    ).toEqual({ available: false, reason: NO_ACTIVE_FOLDER_REASON });
    expect(
      planActiveFolderAsTreeRoot({ activePath: "README.md", browsed: null, ...context })
        .availability,
    ).toEqual({ available: false, reason: ALREADY_ROOT_REASON });
  });
});

describe("planWorkingFolderReturn", () => {
  it("is offered only while the tree browses another folder", () => {
    expect(planWorkingFolderReturn({ browsed: "/repo/src", workspaceRoot: "/repo" })).toEqual({
      availability: { available: true },
      root: null,
    });
    for (const browsed of [null, "/repo", "/repo/"]) {
      expect(planWorkingFolderReturn({ browsed, workspaceRoot: "/repo" }).availability).toEqual({
        available: false,
        reason: AT_WORKING_FOLDER_REASON,
      });
    }
  });
});

describe("showTreeRoot", () => {
  it("remembers the root and asks the session's page, and only it, to show its tree", () => {
    const shown = vi.fn();
    const other = vi.fn();
    renderHook(() => useFilesTreeShowRequests("conv_root", shown));
    renderHook(() => useFilesTreeShowRequests("conv_other", other));

    showTreeRoot("conv_root", "/repo/src");
    expect(rememberedBrowseLocation("conv_root")).toBe("/repo/src");
    expect(shown).toHaveBeenCalledTimes(1);
    expect(other).not.toHaveBeenCalled();

    showTreeRoot("conv_root", null);
    expect(rememberedBrowseLocation("conv_root")).toBeNull();
    rememberBrowseLocation("conv_root", null);
  });
});
