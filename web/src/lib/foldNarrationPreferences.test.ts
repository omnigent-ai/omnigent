import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  readFoldAssistantNarration,
  useFoldAssistantNarration,
  writeFoldAssistantNarration,
} from "./foldNarrationPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("foldNarrationPreferences", () => {
  it("is off by default and only on for the exact persisted value", () => {
    expect(readFoldAssistantNarration()).toBe(false);
    localStorage.setItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY, "yes");
    expect(readFoldAssistantNarration()).toBe(false);

    writeFoldAssistantNarration(true);
    expect(localStorage.getItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY)).toBe("true");
    expect(readFoldAssistantNarration()).toBe(true);

    writeFoldAssistantNarration(false);
    expect(localStorage.getItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY)).toBeNull();
  });

  it("falls back to the default when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => writeFoldAssistantNarration(true)).not.toThrow();
    expect(readFoldAssistantNarration()).toBe(false);
  });

  it("updates mounted readers on a local write and on another window's write", () => {
    const { result } = renderHook(() => useFoldAssistantNarration());
    expect(result.current).toBe(false);

    act(() => writeFoldAssistantNarration(true));
    expect(result.current).toBe(true);

    act(() => {
      localStorage.removeItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY);
      window.dispatchEvent(
        new StorageEvent("storage", { key: FOLD_ASSISTANT_NARRATION_STORAGE_KEY }),
      );
    });
    expect(result.current).toBe(false);

    act(() => {
      localStorage.setItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY, "true");
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
          storageArea: sessionStorage,
        }),
      );
    });
    expect(result.current).toBe(false);

    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: localStorage }));
    });
    expect(result.current).toBe(true);
  });
});
