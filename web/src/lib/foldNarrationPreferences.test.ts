import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import {
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  readFoldAssistantNarration,
  useFoldAssistantNarration,
  writeFoldAssistantNarration,
} from "./foldNarrationPreferences";

// Storage failures and event filtering are covered by storedBooleanPreference.test.ts.
afterEach(() => {
  localStorage.clear();
});

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

it("updates a mounted reader on a local write and on another window's write", () => {
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
});
