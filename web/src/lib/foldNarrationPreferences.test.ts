import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  readFoldAssistantNarration,
  useFoldAssistantNarration,
  writeFoldAssistantNarration,
} from "./foldNarrationPreferences";
import {
  SHOW_TOOL_RESULT_IMAGES_STORAGE_KEY,
  readShowToolResultImages,
  useShowToolResultImages,
  writeShowToolResultImages,
} from "./toolResultImagePreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

// Both opt-in chat toggles share one boolean-preference implementation.
describe.each([
  {
    label: "foldNarrationPreferences",
    storageKey: FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
    read: readFoldAssistantNarration,
    write: writeFoldAssistantNarration,
    usePreference: useFoldAssistantNarration,
  },
  {
    label: "toolResultImagePreferences",
    storageKey: SHOW_TOOL_RESULT_IMAGES_STORAGE_KEY,
    read: readShowToolResultImages,
    write: writeShowToolResultImages,
    usePreference: useShowToolResultImages,
  },
])("$label", ({ storageKey, read, write, usePreference }) => {
  it("is off by default and only on for the exact persisted value", () => {
    expect(read()).toBe(false);
    localStorage.setItem(storageKey, "yes");
    expect(read()).toBe(false);

    write(true);
    expect(localStorage.getItem(storageKey)).toBe("true");
    expect(read()).toBe(true);

    write(false);
    expect(localStorage.getItem(storageKey)).toBeNull();
  });

  it("falls back to the default when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => write(true)).not.toThrow();
    expect(read()).toBe(false);
  });

  it("updates mounted readers on a local write and on another window's write", () => {
    const { result } = renderHook(() => usePreference());
    expect(result.current).toBe(false);

    act(() => write(true));
    expect(result.current).toBe(true);

    act(() => {
      localStorage.removeItem(storageKey);
      window.dispatchEvent(new StorageEvent("storage", { key: storageKey }));
    });
    expect(result.current).toBe(false);

    act(() => {
      localStorage.setItem(storageKey, "true");
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: storageKey,
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
