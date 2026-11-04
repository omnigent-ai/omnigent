import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createStoredBooleanPreference } from "./storedBooleanPreference";

const STORAGE_KEY = "omnigent:test-boolean-preference";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe.each([false, true])("createStoredBooleanPreference defaulting to %s", (defaultValue) => {
  const preference = createStoredBooleanPreference(STORAGE_KEY, defaultValue);
  const otherValue = !defaultValue;

  it("stores only the non-default value and reads only its exact form", () => {
    expect(preference.read()).toBe(defaultValue);
    localStorage.setItem(STORAGE_KEY, "yes");
    expect(preference.read()).toBe(defaultValue);

    preference.write(otherValue);
    expect(localStorage.getItem(STORAGE_KEY)).toBe(String(otherValue));
    expect(preference.read()).toBe(otherValue);

    preference.write(defaultValue);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("falls back to the default when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => preference.write(otherValue)).not.toThrow();
    expect(preference.read()).toBe(defaultValue);
  });

  it("keeps the stored value when removing it fails", () => {
    localStorage.setItem(STORAGE_KEY, String(otherValue));
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => preference.write(defaultValue)).not.toThrow();
    expect(preference.read()).toBe(otherValue);
  });

  it("updates mounted readers on a local write and on another window's write", () => {
    const { result } = renderHook(() => preference.use());
    expect(result.current).toBe(defaultValue);

    act(() => preference.write(otherValue));
    expect(result.current).toBe(otherValue);

    act(() => {
      localStorage.removeItem(STORAGE_KEY);
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    });
    expect(result.current).toBe(defaultValue);

    // sessionStorage changes and other keys are not this preference.
    act(() => {
      localStorage.setItem(STORAGE_KEY, String(otherValue));
      window.dispatchEvent(
        new StorageEvent("storage", { key: STORAGE_KEY, storageArea: sessionStorage }),
      );
      window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:another-preference" }));
    });
    expect(result.current).toBe(defaultValue);

    // A cleared or replaced store (key null) is re-read.
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: localStorage }));
    });
    expect(result.current).toBe(otherValue);

    // A same-tab write that bypasses `write` (settings import) is announced by notifyChanged.
    act(() => {
      localStorage.removeItem(STORAGE_KEY);
      preference.notifyChanged();
    });
    expect(result.current).toBe(defaultValue);
  });
});
