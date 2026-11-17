import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  CODE_BLOCK_WORD_WRAP_STORAGE_KEY,
  readCodeBlockWordWrap,
  toggleCodeBlockWordWrap,
  useCodeBlockWordWrap,
  writeCodeBlockWordWrap,
} from "./codeWrapPreferences";

// Covers the key and boolean mapping; storedChoicePreference.test.ts covers shared storage.
afterEach(() => {
  vi.restoreAllMocks();
  writeCodeBlockWordWrap(true);
  localStorage.clear();
});

it("wraps by default and only stores the non-default choice", () => {
  expect(readCodeBlockWordWrap()).toBe(true);
  localStorage.setItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY, "no");
  expect(readCodeBlockWordWrap()).toBe(true);

  writeCodeBlockWordWrap(false);
  expect(localStorage.getItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY)).toBe("false");
  expect(readCodeBlockWordWrap()).toBe(false);

  toggleCodeBlockWordWrap();
  expect(localStorage.getItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY)).toBeNull();
  expect(readCodeBlockWordWrap()).toBe(true);
});

it("updates every mounted reader on a toggle and on another window's write", () => {
  const first = renderHook(() => useCodeBlockWordWrap());
  const second = renderHook(() => useCodeBlockWordWrap());
  expect(first.result.current).toBe(true);

  act(() => toggleCodeBlockWordWrap());
  expect(first.result.current).toBe(false);
  expect(second.result.current).toBe(false);

  act(() => {
    localStorage.removeItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY);
    window.dispatchEvent(new StorageEvent("storage", { key: CODE_BLOCK_WORD_WRAP_STORAGE_KEY }));
  });
  expect(first.result.current).toBe(true);
  expect(second.result.current).toBe(true);
});

it("keeps the toggle working for this tab when storage is unavailable", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("access denied");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("access denied");
  });
  const { result } = renderHook(() => useCodeBlockWordWrap());
  expect(result.current).toBe(true);

  act(() => toggleCodeBlockWordWrap());
  expect(result.current).toBe(false);
  act(() => toggleCodeBlockWordWrap());
  expect(result.current).toBe(true);
});

it("shows the unsaved choice when saving it fails", () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota exceeded");
  });
  writeCodeBlockWordWrap(false);
  expect(localStorage.getItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY)).toBeNull();
  expect(readCodeBlockWordWrap()).toBe(false);

  vi.restoreAllMocks();
  writeCodeBlockWordWrap(false);
  expect(localStorage.getItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY)).toBe("false");
  localStorage.removeItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY);
  expect(readCodeBlockWordWrap()).toBe(true);
});
