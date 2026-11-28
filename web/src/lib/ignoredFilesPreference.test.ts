import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import {
  readShowIgnoredFiles,
  subscribeShowIgnoredFiles,
  useShowIgnoredFiles,
  writeShowIgnoredFiles,
} from "./ignoredFilesPreference";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("ignored files preference", () => {
  it("hides git-ignored files until the user shows them", () => {
    expect(readShowIgnoredFiles()).toBe(false);
    expect(writeShowIgnoredFiles(true)).toBe(true);
    expect(readShowIgnoredFiles()).toBe(true);
    expect(writeShowIgnoredFiles(false)).toBe(true);
    expect(readShowIgnoredFiles()).toBe(false);
  });

  it("tells this window's listeners, and hears other windows", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeShowIgnoredFiles(listener);
    writeShowIgnoredFiles(true);
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent.files.showIgnored" }));
    window.dispatchEvent(new StorageEvent("storage", { key: "something.else" }));
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    writeShowIgnoredFiles(false);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("keeps a hook current", () => {
    const { result } = renderHook(() => useShowIgnoredFiles());
    expect(result.current).toBe(false);
    act(() => {
      writeShowIgnoredFiles(true);
    });
    expect(result.current).toBe(true);
  });

  it("reports storage it can't write, and reads unreadable storage as hidden", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(writeShowIgnoredFiles(true)).toBe(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readShowIgnoredFiles()).toBe(false);
  });
});
