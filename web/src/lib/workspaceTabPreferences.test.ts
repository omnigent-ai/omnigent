import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_WORKSPACE_TAB,
  normalizeDefaultWorkspaceTab,
  readDefaultWorkspaceTab,
  writeDefaultWorkspaceTab,
} from "./workspaceTabPreferences";

import { readSessionWorkspaceState, writeSessionWorkspaceState } from "./sessionWorkspaceState";

const STORAGE_KEY = "omnigent:default-workspace-tab";

afterEach(() => {
  localStorage.clear();
});

describe("workspaceTabPreferences — read/write", () => {
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
