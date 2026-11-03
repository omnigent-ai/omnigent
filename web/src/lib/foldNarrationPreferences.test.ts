import { afterEach, expect, it } from "vitest";
import {
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  readFoldAssistantNarration,
  writeFoldAssistantNarration,
} from "./foldNarrationPreferences";

// Storage failures and live updates are covered by storedBooleanPreference.test.ts.
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
