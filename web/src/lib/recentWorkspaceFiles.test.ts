import { beforeEach, describe, expect, it } from "vitest";

import {
  RECENT_FILE_SESSIONS,
  RECENT_FILES_PER_SESSION,
  RECENT_WORKSPACE_FILES_STORAGE_KEY,
  readRecentWorkspaceFiles,
  recordRecentWorkspaceFile,
} from "./recentWorkspaceFiles";

describe("recentWorkspaceFiles", () => {
  beforeEach(() => window.localStorage.clear());

  it("lists a session's files most recent first, without repeats", () => {
    recordRecentWorkspaceFile("conv_a", "src/one.ts");
    recordRecentWorkspaceFile("conv_a", "src/two.ts");
    recordRecentWorkspaceFile("conv_a", "src/one.ts");
    recordRecentWorkspaceFile("conv_b", "other.md");
    expect(readRecentWorkspaceFiles("conv_a")).toEqual(["src/one.ts", "src/two.ts"]);
    expect(readRecentWorkspaceFiles("conv_b")).toEqual(["other.md"]);
    expect(readRecentWorkspaceFiles("conv_unknown")).toEqual([]);
  });

  it("keeps the most recent files of the most recently used sessions only", () => {
    for (let index = 0; index <= RECENT_FILES_PER_SESSION; index += 1) {
      recordRecentWorkspaceFile("conv_busy", `file-${index}.ts`);
    }
    const files = readRecentWorkspaceFiles("conv_busy");
    expect(files).toHaveLength(RECENT_FILES_PER_SESSION);
    expect(files[0]).toBe(`file-${RECENT_FILES_PER_SESSION}.ts`);
    expect(files).not.toContain("file-0.ts");

    for (let index = 0; index < RECENT_FILE_SESSIONS; index += 1) {
      recordRecentWorkspaceFile(`conv_${index}`, "a.ts");
    }
    expect(readRecentWorkspaceFiles("conv_busy")).toEqual([]);
    expect(readRecentWorkspaceFiles(`conv_${RECENT_FILE_SESSIONS - 1}`)).toEqual(["a.ts"]);
  });

  it("starts over from corrupt storage", () => {
    window.localStorage.setItem(RECENT_WORKSPACE_FILES_STORAGE_KEY, "{not json");
    expect(readRecentWorkspaceFiles("conv_a")).toEqual([]);
    recordRecentWorkspaceFile("conv_a", "a.ts");
    expect(readRecentWorkspaceFiles("conv_a")).toEqual(["a.ts"]);
  });
});
