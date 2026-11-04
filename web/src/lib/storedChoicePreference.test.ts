import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { createStoredChoicePreference } from "./storedChoicePreference";

const STORAGE_KEY = "omnigent:test-choice-preference";
const preference = createStoredChoicePreference(
  STORAGE_KEY,
  ["small", "medium", "large"],
  "medium",
);

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it("stores only a non-default choice and reads only the listed choices", () => {
  expect(preference.read()).toBe("medium");
  localStorage.setItem(STORAGE_KEY, "huge");
  expect(preference.read()).toBe("medium");

  preference.write("small");
  expect(localStorage.getItem(STORAGE_KEY)).toBe("small");
  expect(preference.read()).toBe("small");
  preference.write("large");
  expect(localStorage.getItem(STORAGE_KEY)).toBe("large");

  preference.write("medium");
  expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
});

it("falls back to the default when storage is unavailable", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("access denied");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota exceeded");
  });
  expect(() => preference.write("large")).not.toThrow();
  expect(preference.read()).toBe("medium");
});

it("keeps the stored choice when removing it fails", () => {
  localStorage.setItem(STORAGE_KEY, "large");
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
    throw new Error("access denied");
  });
  expect(() => preference.write("medium")).not.toThrow();
  expect(preference.read()).toBe("large");
});

it("updates mounted readers on a local write and on another window's write", () => {
  const { result } = renderHook(() => preference.use());
  expect(result.current).toBe("medium");

  act(() => preference.write("large"));
  expect(result.current).toBe("large");

  act(() => {
    localStorage.removeItem(STORAGE_KEY);
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
  });
  expect(result.current).toBe("medium");

  // sessionStorage changes and other keys are not this preference.
  act(() => {
    localStorage.setItem(STORAGE_KEY, "small");
    window.dispatchEvent(
      new StorageEvent("storage", { key: STORAGE_KEY, storageArea: sessionStorage }),
    );
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:another-preference" }));
  });
  expect(result.current).toBe("medium");

  // A cleared or replaced store (key null) is re-read.
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: localStorage }));
  });
  expect(result.current).toBe("small");

  // A same-tab write that bypasses `write` (settings import) is announced by notifyChanged.
  act(() => {
    localStorage.removeItem(STORAGE_KEY);
    preference.notifyChanged();
  });
  expect(result.current).toBe("medium");
});
