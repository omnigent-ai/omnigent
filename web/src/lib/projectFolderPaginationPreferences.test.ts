import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PROJECT_FOLDER_PAGINATION_STORAGE_KEY,
  readProjectFolderPagination,
  useProjectFolderPagination,
  writeProjectFolderPagination,
} from "./projectFolderPaginationPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("projectFolderPaginationPreferences", () => {
  it("is on by default and only off for the exact persisted value", () => {
    expect(readProjectFolderPagination()).toBe(true);
    localStorage.setItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY, "no");
    expect(readProjectFolderPagination()).toBe(true);

    writeProjectFolderPagination(false);
    expect(localStorage.getItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY)).toBe("false");
    expect(readProjectFolderPagination()).toBe(false);

    writeProjectFolderPagination(true);
    expect(localStorage.getItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY)).toBeNull();
  });

  it("falls back to the default when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => writeProjectFolderPagination(false)).not.toThrow();
    expect(readProjectFolderPagination()).toBe(true);
  });

  it("updates mounted readers on a local write and on another window's write", () => {
    const { result } = renderHook(() => useProjectFolderPagination());
    expect(result.current).toBe(true);

    act(() => writeProjectFolderPagination(false));
    expect(result.current).toBe(false);

    act(() => {
      localStorage.removeItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY);
      window.dispatchEvent(
        new StorageEvent("storage", { key: PROJECT_FOLDER_PAGINATION_STORAGE_KEY }),
      );
    });
    expect(result.current).toBe(true);
  });
});
