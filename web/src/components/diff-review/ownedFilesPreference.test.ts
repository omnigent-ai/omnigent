import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readOwnedFilesOnly,
  resetUnsavedOwnedFilesOnly,
  writeOwnedFilesOnly,
} from "./ownedFilesPreference";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  resetUnsavedOwnedFilesOnly();
});

describe("owned files preference", () => {
  it("remembers the choice for its review only, and forgets it when turned off", () => {
    expect(readOwnedFilesOnly("pr-1")).toBe(false);
    expect(writeOwnedFilesOnly("pr-1", true)).toBe(true);
    expect(readOwnedFilesOnly("pr-1")).toBe(true);
    expect(readOwnedFilesOnly("pr-2")).toBe(false);
    writeOwnedFilesOnly("pr-1", false);
    expect(readOwnedFilesOnly("pr-1")).toBe(false);
    expect(window.localStorage.getItem("omnigent.diffReview.ownedFilesOnly")).toBe("[]");
  });

  it("keeps the choice for this session when storage refuses it", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(writeOwnedFilesOnly("pr-1", true)).toBe(false);
    expect(readOwnedFilesOnly("pr-1")).toBe(true);
  });
});
