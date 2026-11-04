import { afterEach, expect, it } from "vitest";
import {
  PROJECT_FOLDER_PAGINATION_STORAGE_KEY,
  readProjectFolderPagination,
  writeProjectFolderPagination,
} from "./projectFolderPaginationPreferences";

// Storage failures and live updates are covered by storedChoicePreference.test.ts.
afterEach(() => {
  localStorage.clear();
});

it("lists the first page by default and stores only the All choice", () => {
  expect(readProjectFolderPagination()).toBe("first-page");
  localStorage.setItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY, "false");
  expect(readProjectFolderPagination()).toBe("first-page");

  writeProjectFolderPagination("all");
  expect(localStorage.getItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY)).toBe("all");
  expect(readProjectFolderPagination()).toBe("all");

  writeProjectFolderPagination("first-page");
  expect(localStorage.getItem(PROJECT_FOLDER_PAGINATION_STORAGE_KEY)).toBeNull();
});
