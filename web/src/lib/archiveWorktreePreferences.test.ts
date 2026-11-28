import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY,
  readDeleteWorktreesOnArchive,
  writeDeleteWorktreesOnArchive,
} from "./archiveWorktreePreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("archiveWorktreePreferences", () => {
  it("reads null when the user has never chosen", () => {
    expect(readDeleteWorktreesOnArchive()).toBeNull();
  });

  it("round-trips both explicit choices", () => {
    writeDeleteWorktreesOnArchive(true);
    expect(readDeleteWorktreesOnArchive()).toBe(true);
    writeDeleteWorktreesOnArchive(false);
    expect(readDeleteWorktreesOnArchive()).toBe(false);
  });

  it("treats an unrecognised stored value as unset", () => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "1");
    expect(readDeleteWorktreesOnArchive()).toBeNull();
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readDeleteWorktreesOnArchive()).toBeNull();
    expect(() => writeDeleteWorktreesOnArchive(true)).not.toThrow();
  });
});
