import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_WORKSPACE_TAB,
  defaultWorkspaceTabs,
  normalizeDefaultWorkspaceTab,
  PINNED_WORKSPACE_TABS_STORAGE_KEY,
  readDefaultWorkspaceTab,
  readInitialWorkspaceTab,
  readPinnedWorkspaceTabs,
  writeDefaultWorkspaceTab,
  writePinnedWorkspaceTabs,
} from "./workspaceTabPreferences";

import {
  type SessionWorkspaceState,
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";

const STORAGE_KEY = "omnigent:default-workspace-tab";

afterEach(() => {
  localStorage.clear();
});

describe("workspaceTabPreferences — read/write", () => {
  it("persists pins, clears stale selections, and never defaults to an unpinned page", () => {
    expect(readPinnedWorkspaceTabs()).toEqual(defaultWorkspaceTabs);
    writeSessionWorkspaceState("conv", { rightRailTab: "files", open: true });
    writePinnedWorkspaceTabs(["subagents", "github", "github"]);
    expect(readPinnedWorkspaceTabs()).toEqual(["github", "subagents"]);
    expect(readInitialWorkspaceTab()).toBe("github");
    expect(readSessionWorkspaceState("conv")).toEqual({ open: true });

    writeDefaultWorkspaceTab("subagents");
    expect(readInitialWorkspaceTab()).toBe("subagents");
    writePinnedWorkspaceTabs([]);
    expect(readPinnedWorkspaceTabs()).toEqual([]);
    expect(readInitialWorkspaceTab()).toBeNull();

    writePinnedWorkspaceTabs(defaultWorkspaceTabs);
    expect(localStorage.getItem(PINNED_WORKSPACE_TABS_STORAGE_KEY)).toBeNull();
    expect(readPinnedWorkspaceTabs()).toEqual(defaultWorkspaceTabs);
  });

  it.each([
    ["invalid-json", defaultWorkspaceTabs],
    ["null", defaultWorkspaceTabs],
    ["{}", defaultWorkspaceTabs],
    ['"files"', defaultWorkspaceTabs],
    ["[]", []],
    ['["unknown"]', []],
    ['["github","unknown","changes","github",null]', ["changes", "github"]],
  ] as const)("reads supported pins from storage: %s", (stored, expected) => {
    localStorage.setItem(PINNED_WORKSPACE_TABS_STORAGE_KEY, stored);
    expect(readPinnedWorkspaceTabs()).toEqual(expected);
  });

  it("preserves an opened page when pinning it and other opened pages when unpinning it again", () => {
    writePinnedWorkspaceTabs(["files", "subagents"]);
    const state: SessionWorkspaceState = {
      rightRailTab: "changes",
      openPanels: ["changes", "github"],
      open: false,
    };
    writeSessionWorkspaceState("conv", state);

    writePinnedWorkspaceTabs(["files", "changes", "subagents"]);
    expect(readSessionWorkspaceState("conv")).toEqual(state);
    writePinnedWorkspaceTabs(["files", "subagents"]);
    expect(readSessionWorkspaceState("conv")).toEqual({ openPanels: ["github"], open: false });
  });

  it("uses a pinned fallback without changing the preferred default or unrelated selections", () => {
    const state: SessionWorkspaceState = {
      rightRailTab: "files",
      selectedFilePath: "README.md",
      openFiles: ["README.md"],
    };
    writeSessionWorkspaceState("conv", state);

    writePinnedWorkspaceTabs(["changes", "github", "subagents"]);

    expect(readDefaultWorkspaceTab()).toBe("files");
    expect(readInitialWorkspaceTab()).toBe("changes");
    expect(readSessionWorkspaceState("conv")).toEqual({
      selectedFilePath: "README.md",
      openFiles: ["README.md"],
    });
    writePinnedWorkspaceTabs(defaultWorkspaceTabs);
    expect(readInitialWorkspaceTab()).toBe("files");
  });

  it("returns files when nothing is stored", () => {
    expect(readDefaultWorkspaceTab()).toBe(DEFAULT_WORKSPACE_TAB);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores a non-default tab and clears the key for files", () => {
    writeDefaultWorkspaceTab("subagents");
    expect(readDefaultWorkspaceTab()).toBe("subagents");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("subagents");

    writeDefaultWorkspaceTab("files");
    expect(readDefaultWorkspaceTab()).toBe("files");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("applies a changed default to existing chats without closing their tabs or changing layout", () => {
    writeSessionWorkspaceState("conv_files", {
      open: true,
      widthPx: 480,
      rightRailTab: "files",
      openFiles: ["README.md"],
      selectedFilePath: "README.md",
    });
    writeSessionWorkspaceState("conv_shell", {
      open: false,
      rightRailTab: "changes",
      openTerminals: ["terminal:main"],
      selectedTerminalKey: "terminal:main",
      openBrowsers: ["browser:one"],
      selectedBrowserId: "browser:one",
    });

    writeDefaultWorkspaceTab("github");

    expect(readDefaultWorkspaceTab()).toBe("github");
    expect(readSessionWorkspaceState("conv_files")).toEqual({
      open: true,
      widthPx: 480,
      openFiles: ["README.md"],
    });
    expect(readSessionWorkspaceState("conv_shell")).toEqual({
      open: false,
      openTerminals: ["terminal:main"],
      openBrowsers: ["browser:one"],
    });

    writeSessionWorkspaceState("conv_files", { rightRailTab: "subagents" });
    writeDefaultWorkspaceTab("github");
    expect(readSessionWorkspaceState("conv_files").rightRailTab).toBe("subagents");

    writeDefaultWorkspaceTab("files");
    expect(readSessionWorkspaceState("conv_files").rightRailTab).toBeUndefined();
  });
});

describe("normalizeDefaultWorkspaceTab", () => {
  it.each(["files", "changes", "github", "subagents"] as const)("passes through %s", (value) => {
    expect(normalizeDefaultWorkspaceTab(value)).toBe(value);
  });

  it.each(["browser", "terminals", "todos", "unknown", null, undefined])(
    "maps %s to files",
    (value) => {
      expect(normalizeDefaultWorkspaceTab(value)).toBe("files");
    },
  );
});
