import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_WORKSPACE_TAB,
  normalizeDefaultWorkspaceTab,
  readDefaultWorkspaceTab,
  writeDefaultWorkspaceTab,
} from "./workspaceTabPreferences";

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
});

describe("normalizeDefaultWorkspaceTab", () => {
  it.each(["files", "subagents", "terminals", "todos", "browser"] as const)(
    "passes through %s",
    (value) => {
      expect(normalizeDefaultWorkspaceTab(value)).toBe(value);
    },
  );

  it.each(["changes", "unknown", null, undefined])("maps %s to files", (value) => {
    expect(normalizeDefaultWorkspaceTab(value)).toBe("files");
  });
});
