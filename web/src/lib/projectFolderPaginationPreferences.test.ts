import { afterEach, expect, it } from "vitest";
import {
  PROJECT_FOLDER_PAGINATION_STORAGE_KEY,
  readProjectFolderPagination,
  writeProjectFolderPagination,
} from "./projectFolderPaginationPreferences";

// Storage failures and live updates are covered by storedBooleanPreference.test.ts.
afterEach(() => {
  localStorage.clear();
});

it("is on by default and only off for the exact persisted value", () => {
  expect(readProjectFolderPagination()).toBe(true);
  localStorage.setItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY, "no");
  expect(readProjectFolderPagination()).toBe(true);

  writeProjectFolderPagination(false);
  expect(localStorage.getItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY)).toBe("false");
  expect(readProjectFolderPagination()).toBe(false);

  writeProjectFolderPagination(true);
  expect(localStorage.getItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY)).toBeNull();
});
