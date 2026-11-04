import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createBooleanPreference } from "./booleanPreference";
import {
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  readFoldAssistantNarration,
  writeFoldAssistantNarration,
} from "./foldNarrationPreferences";
import {
  SHOW_TOOL_RESULT_IMAGES_STORAGE_KEY,
  readShowToolResultImages,
  writeShowToolResultImages,
} from "./toolResultImagePreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("createBooleanPreference", () => {
  it("stores only the override for a default-on preference", () => {
    const preference = createBooleanPreference("test:default-on", true);
    expect(preference.read()).toBe(true);
    localStorage.setItem("test:default-on", "yes");
    expect(preference.read()).toBe(true);

    preference.write(false);
    expect(localStorage.getItem("test:default-on")).toBe("false");
    expect(preference.read()).toBe(false);

    preference.write(true);
    expect(localStorage.getItem("test:default-on")).toBeNull();
  });

  it("falls back to the default when storage is unavailable", () => {
    const preference = createBooleanPreference("test:unavailable", false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => preference.write(true)).not.toThrow();
    expect(preference.read()).toBe(false);
  });

  it("updates mounted readers on writes from this window and others", () => {
    const storageKey = "test:readers";
    const preference = createBooleanPreference(storageKey, false);
    const { result } = renderHook(() => preference.use());
    expect(result.current).toBe(false);

    act(() => preference.write(true));
    expect(result.current).toBe(true);

    act(() => {
      localStorage.removeItem(storageKey);
      window.dispatchEvent(new StorageEvent("storage", { key: storageKey }));
    });
    expect(result.current).toBe(false);

    act(() => {
      localStorage.setItem(storageKey, "true");
      window.dispatchEvent(
        new StorageEvent("storage", { key: storageKey, storageArea: sessionStorage }),
      );
    });
    expect(result.current).toBe(false);

    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: localStorage }));
    });
    expect(result.current).toBe(true);

    // A same-tab direct write (Settings import) fires no event until notified.
    act(() => localStorage.removeItem(storageKey));
    expect(result.current).toBe(true);
    act(() => preference.notify());
    expect(result.current).toBe(false);
  });
});

// Each opt-in chat toggle built on the helper keeps its key and default.
describe.each([
  {
    label: "foldNarrationPreferences",
    storageKey: FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
    read: readFoldAssistantNarration,
    write: writeFoldAssistantNarration,
  },
  {
    label: "toolResultImagePreferences",
    storageKey: SHOW_TOOL_RESULT_IMAGES_STORAGE_KEY,
    read: readShowToolResultImages,
    write: writeShowToolResultImages,
  },
])("$label", ({ storageKey, read, write }) => {
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
});
