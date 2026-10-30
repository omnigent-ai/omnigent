import { afterEach, describe, expect, it, vi } from "vitest";
import { readWorkspacePickerSort, writeWorkspacePickerSort } from "./workspacePickerPreferences";

const STORAGE_KEY = "omnigent:workspace-picker-preferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("workspacePickerPreferences", () => {
  it("defaults to name and round-trips a written sort", () => {
    expect(readWorkspacePickerSort()).toBe("name");
    writeWorkspacePickerSort("modified");
    expect(readWorkspacePickerSort()).toBe("modified");
  });

  it("falls back to name on malformed or unknown stored values", () => {
    for (const raw of ["}{not json", JSON.stringify([1]), JSON.stringify({ sort: "size" })]) {
      localStorage.setItem(STORAGE_KEY, raw);
      expect(readWorkspacePickerSort()).toBe("name");
    }
  });

  it("survives storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => writeWorkspacePickerSort("modified")).not.toThrow();
    expect(readWorkspacePickerSort()).toBe("name");
  });
});
