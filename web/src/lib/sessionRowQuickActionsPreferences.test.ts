import { afterEach, expect, it } from "vitest";
import {
  readSessionRowQuickActions,
  SESSION_ROW_QUICK_ACTIONS_STORAGE_KEY,
  writeSessionRowQuickActions,
} from "./sessionRowQuickActionsPreferences";

// Covers the key and boolean mapping; storedChoicePreference.test.ts covers shared storage.
afterEach(() => {
  localStorage.clear();
});

it("shows the buttons by default and hides them only for the exact persisted value", () => {
  expect(readSessionRowQuickActions()).toBe(true);
  localStorage.setItem(SESSION_ROW_QUICK_ACTIONS_STORAGE_KEY, "no");
  expect(readSessionRowQuickActions()).toBe(true);

  writeSessionRowQuickActions(false);
  expect(localStorage.getItem(SESSION_ROW_QUICK_ACTIONS_STORAGE_KEY)).toBe("false");
  expect(readSessionRowQuickActions()).toBe(false);

  writeSessionRowQuickActions(true);
  expect(localStorage.getItem(SESSION_ROW_QUICK_ACTIONS_STORAGE_KEY)).toBeNull();
});
