import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import {
  CODE_BLOCK_WORD_WRAP_STORAGE_KEY,
  readCodeBlockWordWrap,
  toggleCodeBlockWordWrap,
  useCodeBlockWordWrap,
  writeCodeBlockWordWrap,
} from "./codeWrapPreferences";

// Covers the key and boolean mapping; storedChoicePreference.test.ts covers shared storage.
afterEach(() => {
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
